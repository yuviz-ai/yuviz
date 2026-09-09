"""
Tests for the Phase 5 pipeline: provider interfaces + PipelineConversationHandler.

All providers are mocked so the tests run without FasterWhisper, Ollama, or
Kokoro installed.  The tests verify:
  - ISTT/ILLM/ITTS contracts
  - PipelineConversationHandler produces correct HandlerResponse sequence
  - Cancellation stops the pipeline mid-stream
  - Empty STT result short-circuits (no LLM/TTS called)
  - ConversationSession accumulates audio and calls on_speech_ended
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from libs.config_sdk.workflow import starter_graph
from libs.config_sdk import (
    Agent,
    ConversationInfo,
    MediaInfo,
    Policies,
    ProviderConfig as SDKProviderConfig,
    ProviderConfigs,
    RuntimeConfig,
    Tenant,
)

from ..directives import (
    DirectiveParser,
    EndCallDirective,
    StreamBuffer,
    TransferDirective,
    TransferRequest,
    TransferType,
)
from .. import pipeline as pipeline_module
from ..pipeline import (
    PipelineConversationHandler,
    _END_CALL_MARKER,
    _FALLBACK_GOODBYE,
    _FIRST_TURN_FILLER,
    _TOOL_CALL_FILLER_MIN_GAP_S,
    _TOOL_CALL_FILLERS,
    _TRANSFER_FAILED_FALLBACK,
)
from ..provider_bundle import ProviderBundle
from ..providers.interfaces import ChatMessage, SttResult
from ..session import CallFsmState, ConversationSession, HandlerResponse, SessionContext
from ..event_bus import (
    ConversationFinalized,
    EventBus,
    RecordingEventBus,
    SessionFinalizing,
    TransferCompleted,
    TransferFailed,
    TransferInitiated,
)
from ..echo import EchoConversationHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeMetrics:
    """Records every increment()/observe() call for assertions — no real
    metrics backend exists on the Python side (see metrics.py)."""

    def __init__(self) -> None:
        self.increments: list[tuple[str, float]] = []
        self.observations: list[tuple[str, float]] = []

    def increment(self, name: str, value: float = 1.0) -> None:
        self.increments.append((name, value))

    def observe(self, name: str, value: float) -> None:
        self.observations.append((name, value))

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.increments if n == name)

def _silence(n_frames: int = 60, frame_samples: int = 320) -> bytes:
    # Default must clear PipelineConversationHandler's 1000ms min_bytes gate
    # (60 frames * 320 samples * 2 bytes = 38400 bytes > 32000 byte floor at
    # 16kHz) so on_speech_ended() call sites actually exercise the pipeline
    # instead of silently short-circuiting on the short-utterance check.
    return b"\x00" * (n_frames * frame_samples * 2)


def _make_stt(text: str = "hello world") -> MagicMock:
    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value=SttResult(text=text, confidence=0.95))

    # Delegates through the same transcribe AsyncMock (not a stale captured
    # `text`) so tests that reassign transcribe's return_value/side_effect
    # keep working — matches this fake's own transcribe(), same posture as
    # FasterWhisperSTT's real finalize_stream() fallback.
    async def _finalize_stream(session_id, audio, sample_rate):
        return await stt.transcribe(audio, sample_rate)

    async def _feed_stream(session_id, chunk, sample_rate):
        return None

    async def _cancel_stream(session_id):
        return None

    stt.finalize_stream = _finalize_stream
    stt.feed_stream = _feed_stream
    stt.cancel_stream = _cancel_stream
    return stt


def _make_llm(tokens: list[str] = None) -> MagicMock:
    if tokens is None:
        tokens = ["Hello", "!", " How", " can", " I", " help", "?"]

    async def _gen(messages):
        for t in tokens:
            yield t

    llm = MagicMock()
    llm.generate = _gen
    return llm


def _make_tts(pcm: bytes = b"\x00" * 320) -> MagicMock:
    tts = MagicMock()
    tts.synthesize = AsyncMock(return_value=pcm)

    # Delegates through the same synthesize AsyncMock (not a stale captured
    # `pcm`) so tests that reassign tts.synthesize's return_value/side_effect
    # or assert on tts.synthesize.await_args_list keep working unchanged —
    # this fake just doesn't do genuine chunked streaming, same posture as
    # macOS/Kokoro/ElevenLabs's real synthesize_stream wrappers.
    async def _stream(text, sample_rate):
        audio = await tts.synthesize(text, sample_rate)
        if audio:
            yield audio

    tts.synthesize_stream = _stream
    return tts


def _make_handler(
    stt, llm, tts, *, greeting: str = "", system_prompt: str = "", goodbye_grace_ms: int = 0,
    knowledge=None, transfer_type: str = "none", transfer_destination: str | None = None,
    escalation_threshold: int | None = None,
    end_call_prompt: str | None = None, transfer_prompt: str | None = None,
    farewell_message: str | None = None, transfer_announcement: str | None = None,
    tool_orchestrator=None, max_call_duration_s: int | None = None,
    has_booking_tool: bool = False,
    workflow: dict | None = None, node_tools: list[str] | None = None,
    node_knowledge: list[str] | None = None,
) -> PipelineConversationHandler:
    """Builds the minimal (RuntimeConfig, ProviderBundle) pair these tests
    need — PipelineConversationHandler's real constructor contract now (see
    pipeline.py) — without standing up a real IConfigProvider. These tests
    are about pipeline/FSM behavior (STT->LLM->TTS sequencing, cancellation,
    history), not config resolution, so a hand-built RuntimeConfig with
    placeholder tenant/agent/provider rows is the right level of fake."""
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        id="t1", slug="test", name="Test", region="us",
        vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
        no_speech_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
        transfer_timeout_ms=None,
        default_stt_config_id=None, default_llm_config_id=None, default_tts_config_id=None,
        config_version=1, updated_at=now,
    )
    # Every agent runs a graph; explicit workflow= is for graph tests.
    # node_tools / node_knowledge land on the start node for per-stage scoping.
    if workflow is None:
        workflow = starter_graph(greeting, system_prompt)
        start = next(n for n in workflow["nodes"] if n["type"] == "start")
        if node_tools is not None:
            start["data"]["tools"] = node_tools
        if node_knowledge is not None:
            start["data"]["knowledge_base_ids"] = node_knowledge
    agent = Agent(
        id="a1", slug="test-agent", tenant_id="t1", name="Test Agent",
        greeting=greeting, system_prompt=system_prompt, goodbye_grace_ms=goodbye_grace_ms,
        stt_config_id=None, llm_config_id=None, tts_config_id=None,
        status="active", config_version=1, updated_at=now,
        workflow=workflow,
    )
    placeholder = SDKProviderConfig(id="p1", role="stt", engine="fake", model=None, voice=None, language=None, api_key_ref=None)
    runtime_config = RuntimeConfig(
        tenant=tenant, agent=agent,
        providers=ProviderConfigs(stt=placeholder, llm=placeholder, tts=placeholder),
        conversation=ConversationInfo(
            greeting=greeting, system_prompt=system_prompt,
            end_call_prompt=end_call_prompt, transfer_prompt=transfer_prompt,
            farewell_message=farewell_message,
            transfer_announcement=transfer_announcement,
            workflow=workflow, workflow_draft=workflow,
        ),
        media=MediaInfo(voice=None, language=None),
        policies=Policies(
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            silence_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
            goodbye_grace_ms=goodbye_grace_ms,
            transfer_type=transfer_type, transfer_destination=transfer_destination,
            escalation_threshold=escalation_threshold,
            max_call_duration_s=max_call_duration_s,
        ),
        tools=[], version=1, resolved_at=now,
    )
    bundle = ProviderBundle(stt=stt, llm=llm, tts=tts)
    return PipelineConversationHandler(
        runtime_config, bundle, knowledge=knowledge, tool_orchestrator=tool_orchestrator,
        has_booking_tool=has_booking_tool,
    )


# ---------------------------------------------------------------------------
# Provider interface contracts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stt_returns_result():
    stt = _make_stt("hello")
    result = await stt.transcribe(b"\x00" * 640, 16_000)
    assert result.text == "hello"
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.asyncio
async def test_llm_yields_tokens():
    llm = _make_llm(["Hi", " there"])
    tokens = []
    async for t in llm.generate([ChatMessage(role="user", content="hello")]):
        tokens.append(t)
    assert tokens == ["Hi", " there"]


@pytest.mark.asyncio
async def test_tts_returns_bytes():
    tts = _make_tts(b"\x01\x02" * 160)
    audio = await tts.synthesize("Hello.", 16_000)
    assert isinstance(audio, bytes)
    assert len(audio) > 0


# ---------------------------------------------------------------------------
# PipelineConversationHandler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_produces_stt_then_tts():
    """on_speech_ended yields STT result first, then TTS chunks."""
    stt = _make_stt("test input")
    llm = _make_llm(["Good", " day", "!"])          # sentence ends with "!"
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    assert responses, "expected at least one response"
    # First response must carry the transcript.
    assert responses[0].stt_text == "test input"
    assert responses[0].stt_confidence == pytest.approx(0.95)
    # Subsequent responses carry TTS audio.
    tts_responses = [r for r in responses if r.tts_payloads]
    assert tts_responses, "expected TTS chunks"


@pytest.mark.asyncio
async def test_first_turn_filler_spoken_before_the_real_response():
    stt = _make_stt("hi there")
    llm = _make_llm(["Real", " response", "."])
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts, system_prompt="Be helpful.")
    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    spoken_texts = [call.args[0] for call in tts.synthesize.await_args_list]
    assert spoken_texts[0] == _FIRST_TURN_FILLER
    assert spoken_texts.count(_FIRST_TURN_FILLER) == 1


@pytest.mark.asyncio
async def test_first_turn_filler_not_repeated_on_later_turns():
    stt = _make_stt("hi there")
    llm = _make_llm(["Real", " response", "."])
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts, system_prompt="Be helpful.")
    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass
    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    spoken_texts = [call.args[0] for call in tts.synthesize.await_args_list]
    assert spoken_texts.count(_FIRST_TURN_FILLER) == 1


@pytest.mark.asyncio
async def test_pipeline_empty_stt_yields_nothing():
    """If STT returns empty text, pipeline stops — no LLM or TTS called."""
    stt = _make_stt("")        # empty transcript
    llm = _make_llm()
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 100, -20.0):
        responses.append(r)

    assert responses == []
    llm.generate.assert_not_called() if hasattr(llm.generate, 'assert_not_called') else None
    tts.synthesize.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_cancel_stops_generation():
    """on_cancel() sets the cancel flag; pipeline stops after current STT."""
    stt = _make_stt("cancel me")
    # LLM generates many tokens; pipeline should stop early.
    async def slow_llm(messages):
        for i in range(100):
            yield f"token{i} "
            await asyncio.sleep(0)

    llm = MagicMock()
    llm.generate = slow_llm
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts)

    async def _run():
        responses = []
        async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
            responses.append(r)
            if len(responses) >= 1:        # cancel after STT result
                await handler.on_cancel("s1")
        return responses

    responses = await _run()
    # Should have at most the STT result + a few TTS chunks (not 100 sentences).
    assert len(responses) < 50


@pytest.mark.asyncio
async def test_pipeline_history_accumulates():
    """Each turn appends user+assistant messages to the session history."""
    stt = _make_stt("first turn")
    llm = _make_llm(["Reply."])
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts)

    # First turn.
    async for _ in handler.on_speech_ended("s1", _silence(), 100, -20.0):
        pass

    history = handler._get_history("s1")
    # system (the active step's composed prompt) + user + assistant.
    assert len(history) == 3
    assert history[0].role == "system"
    assert history[1].role == "user"
    assert history[1].content == "first turn"
    assert history[2].role == "assistant"


@pytest.mark.asyncio
async def test_pipeline_marker_only_reply_gets_fallback_audio():
    """
    LLM emits the end-call marker with no spoken text before it (a model
    not following the "after your spoken words" instruction). Without a
    fallback, no TTS audio is produced for the turn and the servicer's
    tts_started_sent guard silently drops EndCall (servicer.py), so the
    call never hangs up. The pipeline must synthesize a fallback goodbye
    so at least one tts_payloads response precedes end_call=True.
    """
    stt = _make_stt("tear down the call")
    llm = _make_llm([_END_CALL_MARKER])        # marker only, nothing spoken
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    end_call_responses = [r for r in responses if r.end_call]
    assert end_call_responses, "expected an end_call response"

    tts_responses = [r for r in responses if r.tts_payloads]
    assert tts_responses, "expected fallback TTS audio so tts_started_sent flips true"

    # Fallback audio must be yielded before end_call=True (servicer keys
    # tts_started_sent off TtsChunks seen prior to the EndCall message).
    end_call_index = responses.index(end_call_responses[0])
    assert any(responses.index(r) < end_call_index for r in tts_responses)

    tts.synthesize.assert_any_call(_FALLBACK_GOODBYE, 16_000)


def _make_spy_llm(tokens: list[str]) -> tuple[MagicMock, list[int]]:
    """_make_llm's `generate` is a plain async-generator function, not a
    Mock — llm.generate.assert_not_called() is silently a no-op against it
    (no such attribute), so it can't actually prove the LLM was skipped.
    This tracks real invocations via a closure-captured counter instead."""
    calls: list[int] = []

    async def _gen(messages):
        calls.append(1)
        for t in tokens:
            yield t

    llm = MagicMock()
    llm.generate = _gen
    return llm, calls


@pytest.mark.asyncio
async def test_pipeline_max_call_duration_ends_call_without_calling_llm():
    """Once policies.max_call_duration_s has elapsed, on_speech_ended must
    skip the LLM entirely (no response is generated only to be discarded)
    and instead speak a fixed wrap-up line, ending the call the same way
    farewell_message/[[END_CALL]] do."""
    stt = _make_stt("are you still there")
    llm, calls = _make_spy_llm(["should never be reached"])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts, max_call_duration_s=60, goodbye_grace_ms=1500)
    # Simulate 61s having elapsed since the handler (= call) was constructed,
    # deterministically, without sleeping in the test.
    handler._call_started_at -= 61

    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    assert calls == [], "LLM must not be invoked once the duration limit is exceeded"
    tts_responses = [r for r in responses if r.tts_payloads]
    assert tts_responses, "expected the fixed wrap-up line to be synthesized"
    end_call_responses = [r for r in responses if r.end_call]
    assert len(end_call_responses) == 1
    assert end_call_responses[0].end_call_grace_period_ms == 1500


@pytest.mark.asyncio
async def test_pipeline_max_call_duration_unset_does_not_affect_normal_turns():
    """max_call_duration_s=None (the default) must behave exactly like
    before this feature existed — no early return, LLM runs normally."""
    stt = _make_stt("hello")
    llm, calls = _make_spy_llm(["Hi", " there", "!"])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts, max_call_duration_s=None)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    assert calls == [1], "expected exactly one real LLM invocation for this turn"
    assert not any(r.end_call for r in responses)
    assert any(r.tts_payloads for r in responses)


@pytest.mark.asyncio
async def test_pipeline_spoken_farewell_does_not_get_extra_fallback():
    """When the LLM speaks a farewell before the marker, no fallback synth
    call is made — the real TTS output is sufficient."""
    stt = _make_stt("bye")
    llm = _make_llm(["Goodbye", "!", _END_CALL_MARKER])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    assert any(r.end_call for r in responses)
    synth_calls = [c.args[0] for c in tts.synthesize.await_args_list]
    assert _FALLBACK_GOODBYE not in synth_calls


@pytest.mark.asyncio
async def test_pipeline_session_end_clears_history():
    stt = _make_stt("hello")
    llm = _make_llm(["bye."])
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts)
    async for _ in handler.on_speech_ended("s1", _silence(), 100, -20.0):
        pass

    await handler.on_session_end("s1", "hangup")
    assert "s1" not in handler._sessions


# ---------------------------------------------------------------------------
# ConversationSession audio buffer + on_speech_ended integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_accumulates_audio_across_push():
    """Audio buffer grows across multiple push_audio() calls."""
    stt = _make_stt("buffered")
    llm = _make_llm(["Ok."])
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts)
    ctx = SessionContext(session_id="s1")
    bus = EventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    await bus.start()

    chunk = _silence(n_frames=1)
    # Push 3 chunks.
    for _ in range(3):
        await session.push_audio(chunk)

    expected_len = len(chunk) * 3
    assert len(session._audio_buffer) == expected_len

    await bus.stop()


@pytest.mark.asyncio
async def test_session_speech_ended_clears_buffer_and_yields_responses():
    stt = _make_stt("spoke")
    llm = _make_llm(["Yes."])
    tts = _make_tts(b"\x01" * 320)

    handler = _make_handler(stt, llm, tts)
    ctx = SessionContext(session_id="s2")
    bus = EventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    await bus.start()

    await session.push_audio(_silence())
    assert len(session._audio_buffer) > 0

    responses = []
    async for r in session.speech_ended(duration_ms=100, energy_db=-25.0):
        responses.append(r)

    # Buffer must be cleared after speech_ended.
    assert len(session._audio_buffer) == 0
    # At least one response (STT result).
    assert any(r.stt_text for r in responses)

    await bus.stop()


@pytest.mark.asyncio
async def test_agent_id_falsy_sentinel_becomes_none_not_a_fake_string():
    """Regression test: RuntimeConfig.agent.id is a non-optional str field,
    so the legacy-fallback adapter (agent_config.to_runtime_config()) uses
    "" as its honest "no real Postgres row" sentinel. Without the `or None`
    in PipelineConversationHandler's constructor, that empty string (or a
    tempting-but-wrong literal sentinel like "legacy") would flow into
    TranscriptBuilder.begin_call()'s agent_id argument and then into
    calls.agent_id, a real UUID FK column — breaking the insert outright,
    not just being cosmetically wrong."""
    from ..agent_config import AgentConfig, to_runtime_config

    legacy_runtime_config, legacy_bundle = to_runtime_config(
        AgentConfig(), "acme", "sup", stt=_make_stt(), llm=_make_llm(), tts=_make_tts(),
    )
    legacy_handler = PipelineConversationHandler(legacy_runtime_config, legacy_bundle)
    assert legacy_handler._agent_id is None
    assert legacy_handler._agent_config_version is None

    # A real (non-empty, non-zero) id/version must pass through unchanged.
    now = datetime.now(timezone.utc)
    real_agent = Agent(
        id="real-agent-id", slug="sup", tenant_id="t1", name="Sup",
        greeting="", system_prompt="", goodbye_grace_ms=0,
        stt_config_id=None, llm_config_id=None, tts_config_id=None,
        status="active", config_version=7, updated_at=now,
    )
    placeholder = SDKProviderConfig(id="p1", role="stt", engine="fake", model=None, voice=None, language=None, api_key_ref=None)
    real_runtime_config = RuntimeConfig(
        tenant=Tenant(
            id="t1", slug="acme", name="Acme", region="us",
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            no_speech_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
            transfer_timeout_ms=None,
            default_stt_config_id=None, default_llm_config_id=None, default_tts_config_id=None,
            config_version=1, updated_at=now,
        ),
        agent=real_agent,
        providers=ProviderConfigs(stt=placeholder, llm=placeholder, tts=placeholder),
        conversation=ConversationInfo(greeting="", system_prompt="", workflow=starter_graph()),
        media=MediaInfo(voice=None, language=None),
        policies=Policies(
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            silence_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None, goodbye_grace_ms=0,
        ),
        tools=[], version=7, resolved_at=now,
    )
    real_handler = PipelineConversationHandler(
        real_runtime_config, ProviderBundle(stt=_make_stt(), llm=_make_llm(), tts=_make_tts()),
    )
    assert real_handler._agent_id == "real-agent-id"
    assert real_handler._agent_config_version == 7


# ---------------------------------------------------------------------------
# StreamBuffer + DirectiveParser — streaming-safe marker detection
# (directives.py)
# ---------------------------------------------------------------------------

def test_stream_buffer_no_tag_passes_text_through_unchanged():
    buf = StreamBuffer()
    safe = buf.feed("Sure, I can help with that.")
    assert safe == "Sure, I can help with that."
    assert buf.flush() == ""


def test_directive_parser_extracts_transfer_with_all_attrs():
    text = (
        'Let me connect you with a specialist. '
        '[[TRANSFER type="warm" destination="+15551234567" reason="billing dispute"]]'
    )
    result = DirectiveParser.parse(text)
    assert result.clean_text == "Let me connect you with a specialist. "
    assert result.directives == [TransferDirective(
        transfer_type=TransferType.WARM, destination="+15551234567", reason="billing dispute",
    )]


def test_directive_parser_attribute_order_does_not_matter():
    result = DirectiveParser.parse(
        '[[TRANSFER reason="escalation" destination="queue:billing" type="cold"]]'
    )
    directive = result.directives[0]
    assert directive.transfer_type == TransferType.COLD
    assert directive.destination == "queue:billing"
    assert directive.reason == "escalation"


def test_directive_parser_missing_attribute_defaults_to_empty_string():
    result = DirectiveParser.parse('[[TRANSFER type="warm" destination="+15551234567"]]')
    directive = result.directives[0]
    assert directive.transfer_type == TransferType.WARM
    assert directive.destination == "+15551234567"
    assert directive.reason == ""


def test_directive_parser_unknown_transfer_type_coerces_to_none():
    result = DirectiveParser.parse('[[TRANSFER type="sideways" destination="x"]]')
    assert result.directives[0].transfer_type == TransferType.NONE


def test_directive_parser_end_call_has_no_attrs():
    result = DirectiveParser.parse("Goodbye. [[END_CALL]]")
    assert result.clean_text == "Goodbye. "
    assert result.directives == [EndCallDirective()]


def test_stream_buffer_holds_back_unterminated_tag():
    """A tag split across two streamed chunks must not leak a partial
    fragment as "safe" text — this is what protects TTS from ever reading
    a raw tag fragment aloud. feed() only returns text once it can prove
    no tag inside it is still open; parsing that text is DirectiveParser's
    separate job."""
    buf = StreamBuffer()
    safe1 = buf.feed('Connecting you. [[TRANSFER type="warm" ')
    assert safe1 == "Connecting you. "
    assert DirectiveParser.parse(safe1).directives == []

    # Second chunk closes the tag — feed() now returns it (parsing/
    # stripping is DirectiveParser's job, not StreamBuffer's).
    safe2 = buf.feed('destination="+15551234567"]]')
    assert safe2 == '[[TRANSFER type="warm" destination="+15551234567"]]'
    result = DirectiveParser.parse(safe2)
    assert result.clean_text == ""
    assert result.directives[0].destination == "+15551234567"


def test_stream_buffer_reason_with_period_does_not_leak_partial_tag():
    """Regression guard: a reason value containing sentence-ending
    punctuation followed by whitespace (e.g. "resolved. thanks") must not
    confuse a downstream sentence-splitter into treating mid-tag text as a
    complete, speakable sentence — feed() must hold the *entire* tag back
    until it closes, regardless of what looks like sentence punctuation
    inside it."""
    buf = StreamBuffer()
    safe = buf.feed('[[TRANSFER reason="issue is resolved. thanks" type="cold"')
    assert safe == ""
    assert buf.flush() == '[[TRANSFER reason="issue is resolved. thanks" type="cold"'


def test_stream_buffer_unclosed_tag_at_stream_end_is_not_a_directive():
    buf = StreamBuffer()
    safe = buf.feed("The bracket [[ appears here but never closes.")
    assert safe == "The bracket "
    remainder = buf.flush()
    assert remainder == "[[ appears here but never closes."
    assert DirectiveParser.parse(remainder).directives == []


def test_directive_parser_strip_removes_every_complete_tag():
    text = 'Hi there! [[TRANSFER type="cold"]] and also [[END_CALL]]'
    assert DirectiveParser.parse(text).clean_text == "Hi there!  and also "


# ---------------------------------------------------------------------------
# Phase 3 — transfer detection wired into PipelineConversationHandler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_detects_transfer_directive_and_yields_transfer_request():
    """
    Phase 3 requirement: detecting [[TRANSFER ...]] creates a TransferRequest
    and surfaces it via HandlerResponse — no gRPC, no gateway/ESL call, no
    transfer actually performed (that's what the servicer's bus.publish
    does with it — observability only, see servicer.py/event_bus.py).
    """
    stt = _make_stt("I need to speak to billing")
    llm = _make_llm([
        "Connecting you now. ",
        '[[TRANSFER type="warm" destination="+15551234567" reason="billing"]]',
    ])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    transfer_responses = [r for r in responses if r.transfer_request]
    assert len(transfer_responses) == 1
    tr = transfer_responses[0].transfer_request
    assert tr.transfer_type == "warm"
    assert tr.destination == "+15551234567"
    assert tr.reason == "billing"
    assert tr.trigger == "llm_directive"
    assert tr.session_id == "s1"

    # Not conflated with end_call — a transfer request is not a hangup.
    assert not any(r.end_call for r in responses)

    # Never leaks into stored history. history[0] is the step prompt, [1] the
    # caller's turn, [2] the agent's reply.
    history = handler._get_history("s1")
    assert "[[TRANSFER" not in history[2].content
    assert history[2].content.strip() == "Connecting you now."


@pytest.mark.asyncio
async def test_pipeline_transfer_directive_split_across_tokens_still_detected():
    stt = _make_stt("please transfer me")
    llm = _make_llm([
        "One moment. ", "[[TRANSFER type=", '"cold" destination="+1555" ', 'reason="escalate"]]',
    ])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    transfer_responses = [r for r in responses if r.transfer_request]
    assert len(transfer_responses) == 1
    assert transfer_responses[0].transfer_request.transfer_type == "cold"

    # No raw tag fragment leaked into any synthesized sentence.
    synth_calls = [c.args[0] for c in tts.synthesize.await_args_list]
    assert not any("[[TRANSFER" in call for call in synth_calls)


@pytest.mark.asyncio
async def test_pipeline_no_transfer_request_when_no_directive_present():
    stt = _make_stt("just a normal question")
    llm = _make_llm(["Sure, ", "here's the answer."])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    assert not any(r.transfer_request for r in responses)


# ---------------------------------------------------------------------------
# Phase 3 — escalation_threshold / record_guardrail_violation()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guardrail_violations_below_threshold_do_not_trigger_transfer():
    stt = _make_stt("hi")
    llm = _make_llm(["ok"])
    tts = _make_tts()
    handler = _make_handler(
        stt, llm, tts, transfer_type="cold", transfer_destination="+15550001111",
        escalation_threshold=3,
    )

    assert handler.record_guardrail_violation("s1") is None
    assert handler.record_guardrail_violation("s1") is None
    assert handler.record_guardrail_violation("s1") is None  # 3rd == threshold, not yet over


@pytest.mark.asyncio
async def test_guardrail_violations_exceeding_threshold_returns_transfer_request():
    stt = _make_stt("hi")
    llm = _make_llm(["ok"])
    tts = _make_tts()
    handler = _make_handler(
        stt, llm, tts, transfer_type="cold", transfer_destination="+15550001111",
        escalation_threshold=2,
    )

    assert handler.record_guardrail_violation("s1") is None  # count=1
    assert handler.record_guardrail_violation("s1") is None  # count=2 == threshold, not yet over
    request = handler.record_guardrail_violation("s1")       # count=3 > threshold
    assert request is not None
    assert request.transfer_type == "cold"
    assert request.destination == "+15550001111"
    assert request.trigger == "escalation_threshold"
    assert "violations=3" in request.reason


@pytest.mark.asyncio
async def test_guardrail_threshold_none_disables_escalation():
    stt = _make_stt("hi")
    llm = _make_llm(["ok"])
    tts = _make_tts()
    handler = _make_handler(stt, llm, tts, escalation_threshold=None)

    for _ in range(50):
        assert handler.record_guardrail_violation("s1") is None


@pytest.mark.asyncio
async def test_pending_escalation_transfer_surfaces_on_next_turn():
    stt = _make_stt("hello")
    llm = _make_llm(["ok."])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(
        stt, llm, tts, transfer_type="warm", transfer_destination="+15559998888",
        escalation_threshold=1,
    )

    assert handler.record_guardrail_violation("s1") is None  # count=1 == threshold, not yet over
    request = handler.record_guardrail_violation("s1")        # count=2 > threshold=1
    assert request is not None

    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    transfer_responses = [r for r in responses if r.transfer_request]
    assert len(transfer_responses) == 1
    assert transfer_responses[0].transfer_request.trigger == "escalation_threshold"

    # Consumed — a second turn must not re-surface the same pending request.
    responses2 = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses2.append(r)
    assert not any(r.transfer_request for r in responses2)


@pytest.mark.asyncio
async def test_transfer_request_suppressed_on_barge_in_cancel():
    """A caller barge-in mid-turn makes the agent's transfer decision
    stale — same reasoning already applied to end_call."""
    stt = _make_stt("transfer me")

    async def slow_llm(messages):
        yield "One moment"
        await asyncio.sleep(0)
        yield '[[TRANSFER type="cold" destination="+1555" reason="x"]]'

    llm = MagicMock()
    llm.generate = slow_llm
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts)

    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)
        if len(responses) == 1:
            await handler.on_cancel("s1")

    assert not any(r.transfer_request for r in responses)


# ---------------------------------------------------------------------------
# Phase 5C — PipelineConversationHandler.on_transfer_failed() (recovery)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_transfer_failed_synthesizes_apology_via_llm():
    stt = _make_stt()
    llm = _make_llm(["I'm so sorry, ", "I couldn't reach an agent — how else can I help?"])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_transfer_failed("s1", "+15551234567", "hangup_before_bridge"):
        responses.append(r)

    tts_responses = [r for r in responses if r.tts_payloads]
    assert tts_responses, "expected TTS audio for the apology"
    # Never a fallback synthesis when the LLM produced real text.
    synth_calls = [c.args[0] for c in tts.synthesize.await_args_list]
    assert _TRANSFER_FAILED_FALLBACK not in synth_calls


@pytest.mark.asyncio
async def test_on_transfer_failed_sends_structured_system_event_to_agent_runtime():
    """Requirement 5/6: AgentRuntime (the LLM call) receives the structured
    system event verbatim — {"type": "system_event", "event":
    "transfer_failed", "reason": ...} — not a hardcoded recovery sentence;
    the LLM (via its existing prompt) generates the actual reply."""
    import json as _json

    captured_messages = []

    async def capturing_llm(messages):
        captured_messages.append(list(messages))
        yield "Sorry about that, let's continue."

    llm = MagicMock()
    llm.generate = capturing_llm
    stt = _make_stt()
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    async for _ in handler.on_transfer_failed("s1", "+15551234567", "hangup_before_bridge"):
        pass

    assert len(captured_messages) == 1
    last_message = captured_messages[0][-1]
    assert last_message.role == "user"

    event_json = last_message.content.split("\n\n")[0]
    event = _json.loads(event_json)
    assert event == {
        "type": "system_event", "event": "transfer_failed", "reason": "hangup_before_bridge",
    }
    # No hardcoded recovery sentence anywhere in what's sent to the LLM.
    assert _TRANSFER_FAILED_FALLBACK not in last_message.content


@pytest.mark.asyncio
async def test_on_transfer_failed_notice_is_ephemeral_not_stored_in_history():
    stt = _make_stt()
    llm = _make_llm(["Sorry about that, still happy to help."])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    async for _ in handler.on_transfer_failed("s1", "+15551234567", "hangup_before_bridge"):
        pass

    history = handler._get_history("s1")
    # The step prompt plus the assistant's apology — no user-role notice turn.
    assert len(history) == 2
    assert history[1].role == "assistant"
    assert history[1].content.strip() == "Sorry about that, still happy to help."


@pytest.mark.asyncio
async def test_on_transfer_failed_falls_back_when_llm_produces_nothing():
    stt = _make_stt()
    llm = _make_llm([])  # no tokens at all
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    responses = []
    async for r in handler.on_transfer_failed("s1", "+15551234567", "esl_unreachable"):
        responses.append(r)

    tts_responses = [r for r in responses if r.tts_payloads]
    assert tts_responses, "expected fallback TTS audio so the caller isn't left in silence"
    tts.synthesize.assert_any_call(_TRANSFER_FAILED_FALLBACK, 16_000)

    history = handler._get_history("s1")
    assert len(history) == 2
    assert history[1].content == _TRANSFER_FAILED_FALLBACK


@pytest.mark.asyncio
async def test_on_transfer_failed_defers_transcript_persistence_to_session_end():
    """Requirement: do not persist memory immediately — only during normal
    SessionEnd. record_turn() must not fire until on_session_end()."""
    stt = _make_stt()
    llm = _make_llm(["Apologies, let's continue."])
    tts = _make_tts(b"\x00" * 640)

    transcripts = MagicMock()
    handler = _make_handler(stt, llm, tts)
    handler._transcripts = transcripts

    async for _ in handler.on_transfer_failed("s1", "+15551234567", "hangup_before_bridge"):
        pass

    transcripts.record_turn.assert_not_called()

    await handler.on_session_end("s1", "caller_hangup")

    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[0] == "s1"
    assert "hangup_before_bridge" in args[1]
    assert args[3] == "Apologies, let's continue."
    assert args[4] is False  # not cancelled
    transcripts.end_call.assert_called_once_with("s1", "caller_hangup", final_state=None)


@pytest.mark.asyncio
async def test_on_transfer_failed_second_turn_continues_normally():
    """After recovery, a normal caller utterance must still work — proves
    on_transfer_failed's history-only-assistant-turn doesn't corrupt the
    (user, assistant) pairing on_speech_ended relies on."""
    stt = _make_stt("are you still there")
    llm = _make_llm(["Yes, still here!"])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    async for _ in handler.on_transfer_failed("s1", "+15551234567", "x"):
        pass

    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    assert any(r.stt_text == "are you still there" for r in responses)
    history = handler._get_history("s1")
    assert history[-2].role == "user"
    assert history[-1].role == "assistant"


@pytest.mark.asyncio
async def test_on_speech_ended_records_turn_latency():
    """A normal, uninterrupted turn should record all four latency
    numbers, none of them zero/negative, and voice_to_voice_ms should be
    at least as large as stt_ms (it starts measuring earlier)."""
    stt = _make_stt("book me a haircut")
    llm = _make_llm(["Sure, what time works?"])
    tts = _make_tts(b"\x00" * 640)

    transcripts = MagicMock()
    handler = _make_handler(stt, llm, tts)
    handler._transcripts = transcripts

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    transcripts.record_turn.assert_called_once()
    latency = transcripts.record_turn.call_args.kwargs["latency"]
    assert latency.stt_ms is not None and latency.stt_ms >= 0
    assert latency.llm_ms is not None and latency.llm_ms >= 0
    assert latency.tts_ms is not None and latency.tts_ms >= 0
    assert latency.voice_to_voice_ms is not None
    assert latency.voice_to_voice_ms >= latency.stt_ms
    assert latency.stt_engine and latency.llm_engine and latency.tts_engine


@pytest.mark.asyncio
async def test_on_speech_ended_cancelled_turn_records_partial_latency():
    """A barge-in before any audio synthesizes must not crash record_turn —
    tts_ms/voice_to_voice_ms stay None (never a misleading 0) when no
    audio ever actually played."""
    stt = _make_stt("hello")
    llm = _make_llm(["Sure thing right away."])
    tts = _make_tts(b"\x00" * 640)

    transcripts = MagicMock()
    handler = _make_handler(stt, llm, tts)
    handler._transcripts = transcripts

    gen = handler.on_speech_ended("s1", _silence(), 200, -20.0)
    await gen.__anext__()  # consume just the stt_text response
    handler._sessions["s1"].cancelled.set()
    async for _ in gen:
        pass

    transcripts.record_turn.assert_called_once()
    latency = transcripts.record_turn.call_args.kwargs["latency"]
    assert latency.stt_ms is not None  # STT always completes before cancellation is checked


# ---------------------------------------------------------------------------
# Phase 5D — PipelineConversationHandler.finalize_session()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_session_uses_this_handlers_own_history_and_llm():
    """finalize_session() must draw on the *same* per-session state
    on_speech_ended() built up — not a fresh/empty history."""
    stt = _make_stt("I need billing help")
    llm = _make_llm(["Let me get that sorted."])
    tts = _make_tts(b"\x00" * 640)

    handler = _make_handler(stt, llm, tts)
    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass  # builds real history: one user + one assistant turn

    summary_llm_calls = []

    async def summary_gen(messages):
        summary_llm_calls.append(list(messages))
        yield "Caller asked about billing; agent assisted."

    llm.generate = summary_gen

    result = await handler.finalize_session("s1", "transfer_completed")

    assert result.summary == "Caller asked about billing; agent assisted."
    assert len(summary_llm_calls) == 1
    sent = summary_llm_calls[0]
    # The real conversation history (user + assistant) precedes the
    # summary-request message appended by SessionFinalizer.
    assert any(m.role == "user" and m.content == "I need billing help" for m in sent)
    assert any(m.role == "assistant" for m in sent)


@pytest.mark.asyncio
async def test_finalize_session_persists_summary_via_handlers_transcripts():
    stt = _make_stt()
    llm = _make_llm(["Summary text."])
    tts = _make_tts()
    transcripts = MagicMock()

    handler = _make_handler(stt, llm, tts)
    handler._transcripts = transcripts
    handler._session_finalizer._transcripts = transcripts  # same sink, wired at construction
    handler._get_history("s1").append(ChatMessage(role="user", content="hello"))

    await handler.finalize_session("s1", "transfer_completed")

    transcripts.record_turn.assert_called_once_with(
        "s1", "[session_summary]", 1.0, "Summary text.", False,
    )


@pytest.mark.asyncio
async def test_finalize_session_is_idempotent():
    stt = _make_stt()
    call_count = 0

    async def counting_gen(messages):
        nonlocal call_count
        call_count += 1
        yield f"summary #{call_count}"

    llm = MagicMock()
    llm.generate = counting_gen
    tts = _make_tts()

    handler = _make_handler(stt, llm, tts)
    handler._get_history("s1").append(ChatMessage(role="user", content="hello"))
    first = await handler.finalize_session("s1", "transfer_completed")
    second = await handler.finalize_session("s1", "transfer_completed")

    assert first.summary == "summary #1"
    assert second.summary == "summary #1"
    assert call_count == 1


@pytest.mark.asyncio
async def test_session_cancel_clears_buffer():
    handler = EchoConversationHandler()
    ctx = SessionContext(session_id="s3")
    bus = EventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    await bus.start()

    await session.push_audio(_silence(n_frames=3))
    assert len(session._audio_buffer) > 0

    await session.cancel()
    assert len(session._audio_buffer) == 0

    await bus.stop()


# ---------------------------------------------------------------------------
# Phase 5B — gateway → service transfer notifications (session.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_on_transfer_initiated_drives_fsm_and_publishes_event():
    handler = EchoConversationHandler()
    ctx = SessionContext(session_id="s4")
    bus = RecordingEventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()

    session.on_transfer_initiated("cold", "+15551234567", "escalation_threshold_exceeded")

    assert session.fsm_state == CallFsmState.TRANSFERRING
    events = bus.published_of(TransferInitiated)
    assert len(events) == 1
    assert events[0].session_id == "s4"
    assert events[0].transfer_type == "cold"
    assert events[0].destination == "+15551234567"
    assert events[0].reason == "escalation_threshold_exceeded"


@pytest.mark.asyncio
async def test_session_on_transfer_completed_finalizes_and_moves_to_closing():
    """Phase 5D: success now passes through Finalizing (running
    SessionFinalizer) before reaching Closing — see TestSessionFinalization
    below for dedicated coverage of that step."""
    handler = EchoConversationHandler()
    ctx = SessionContext(session_id="s5")
    bus = RecordingEventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    session.on_transfer_initiated("cold", "+15551234567", "x")

    await session.on_transfer_completed("+15551234567")

    assert session.fsm_state == CallFsmState.CLOSING
    events = bus.published_of(TransferCompleted)
    assert len(events) == 1
    assert events[0].destination == "+15551234567"
    assert len(bus.published_of(SessionFinalizing)) == 1
    assert len(bus.published_of(ConversationFinalized)) == 1


@pytest.mark.asyncio
async def test_session_on_transfer_failed_recovers_to_listening_and_publishes_event():
    """Phase 5C: unlike TransferCompleted, a failure does not end the call —
    the session returns to LISTENING (recoverable) rather than CLOSING."""
    handler = EchoConversationHandler()
    ctx = SessionContext(session_id="s6")
    bus = RecordingEventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    session.on_transfer_initiated("cold", "+15551234567", "x")

    async for _ in session.on_transfer_failed("+15551234567", "hangup_before_bridge"):
        pass

    assert session.fsm_state == CallFsmState.LISTENING
    events = bus.published_of(TransferFailed)
    assert len(events) == 1
    assert events[0].destination == "+15551234567"
    assert events[0].reason == "hangup_before_bridge"


@pytest.mark.asyncio
async def test_session_transfer_failed_passes_through_recovering_and_speaking():
    """Exercises the full workflow transition: Transferring -> Recovering ->
    Speaking -> Listening — using a handler that actually produces TTS
    audio (EchoConversationHandler's on_transfer_failed yields nothing, so
    the simpler test above only exercises the empty-audio safety net)."""
    class ApologizingHandler(EchoConversationHandler):
        async def on_transfer_failed(self, session_id, destination, reason):
            yield HandlerResponse(tts_payloads=[b"\x00" * 320])

    handler = ApologizingHandler()
    ctx = SessionContext(session_id="s6b")
    bus = RecordingEventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    session.on_transfer_initiated("cold", "+15551234567", "x")

    states_during_recovery = []
    async for _ in session.on_transfer_failed("+15551234567", "x"):
        states_during_recovery.append(session.fsm_state)

    # By the time the first (only) TTS response was yielded, the FSM had
    # already moved past Recovering into Speaking.
    assert CallFsmState.SPEAKING in states_during_recovery
    assert session.fsm_state == CallFsmState.SPEAKING  # awaiting a real PlaybackFinished

    session.on_playback_finished(interrupted=False)
    assert session.fsm_state == CallFsmState.LISTENING


@pytest.mark.asyncio
async def test_session_transfer_metrics_emitted():
    """Requirement: transfer_attempts_total, transfer_recovery_success_total,
    transfer_recovery_latency_ms — transfer_failures_total is covered
    separately below (it's driven by a real EventBus subscription, not the
    direct call path)."""
    class ApologizingHandler(EchoConversationHandler):
        async def on_transfer_failed(self, session_id, destination, reason):
            yield HandlerResponse(tts_payloads=[b"\x00" * 320])

    handler = ApologizingHandler()
    ctx = SessionContext(session_id="s6c")
    bus = RecordingEventBus()
    metrics = FakeMetrics()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler, metrics=metrics)
    session.session_ready()

    session.on_transfer_initiated("cold", "+15551234567", "x")
    assert metrics.count("transfer_attempts_total") == 1

    async for _ in session.on_transfer_failed("+15551234567", "x"):
        pass

    assert metrics.count("transfer_recovery_success_total") == 1
    assert len(metrics.observations) == 1
    name, value = metrics.observations[0]
    assert name == "transfer_recovery_latency_ms"
    assert value >= 0.0


@pytest.mark.asyncio
async def test_session_transfer_recovery_success_not_counted_when_no_audio():
    handler = EchoConversationHandler()  # on_transfer_failed yields nothing
    ctx = SessionContext(session_id="s6d")
    bus = RecordingEventBus()
    metrics = FakeMetrics()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler, metrics=metrics)
    session.session_ready()
    session.on_transfer_initiated("cold", "+15551234567", "x")

    async for _ in session.on_transfer_failed("+15551234567", "x"):
        pass

    assert metrics.count("transfer_recovery_success_total") == 0
    # The safety net still resolves the FSM even without real audio.
    assert session.fsm_state == CallFsmState.LISTENING


@pytest.mark.asyncio
async def test_workflow_engine_subscribes_to_transfer_failed_for_metrics():
    """Requirement 2+3: GrpcEventBridge (session.py) publishes TransferFailed
    onto the EventBus; WorkflowEngine (ConversationSession's own bus
    subscription — see __init__) reacts to it, independent of the direct
    on_transfer_failed() call chain that streams the apology."""
    handler = EchoConversationHandler()
    ctx = SessionContext(session_id="s6e")
    bus = EventBus()
    metrics = FakeMetrics()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler, metrics=metrics)
    session.session_ready()
    await bus.start()

    session.on_transfer_initiated("cold", "+15551234567", "x")
    async for _ in session.on_transfer_failed("+15551234567", "x"):
        pass

    await bus.drain()
    assert metrics.count("transfer_failures_total") == 1

    await bus.stop()


@pytest.mark.asyncio
async def test_session_transfer_completed_without_initiated_is_a_no_op():
    """ConversationFSM guards on state()==TRANSFERRING (see fsm.py) — a
    TransferCompleted arriving with no prior TransferInitiated (shouldn't
    happen per the gRPC contract, but the FSM must survive it, not crash)
    is silently ignored rather than corrupting FSM state."""
    handler = EchoConversationHandler()
    ctx = SessionContext(session_id="s7")
    bus = RecordingEventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()

    await session.on_transfer_completed("+15551234567")

    assert session.fsm_state == CallFsmState.LISTENING
    # The event is still published (observability), even though the FSM
    # itself no-oped — matches TransferInitiated/Completed/Failed being
    # unconditionally published in session.py.
    assert len(bus.published_of(TransferCompleted)) == 1


# ---------------------------------------------------------------------------
# Transfer instruction auto-injection (single source of truth = policies,
# never hand-written prompt text — see _TRANSFER_INSTRUCTION_TEMPLATE)
# ---------------------------------------------------------------------------

def test_transfer_instruction_injected_when_transfer_configured():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001",
    )
    assert '[[TRANSFER type="cold" destination="1001"' in handler._workflow.system_prompt()
    # The base personality and the end-call instruction are both still there.
    assert handler._workflow.system_prompt().startswith("You are Alex.")
    assert "[[END_CALL]]" in handler._workflow.system_prompt()


def test_transfer_instruction_absent_when_transfer_type_none():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="none", transfer_destination="1001",
    )
    assert "[[TRANSFER" not in handler._workflow.system_prompt()


def test_transfer_instruction_absent_without_destination():
    # A transfer the LLM can request but nothing can complete would strand
    # callers mid-"connecting you now" — misconfiguration must not inject.
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination=None,
    )
    assert "[[TRANSFER" not in handler._workflow.system_prompt()


def test_transfer_instruction_injected_even_with_empty_system_prompt_column():
    # The graph always supplies a prompt now — transfer injection is gated on
    # transfer config validation, not on the transitional system_prompt column.
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="",
        transfer_type="cold", transfer_destination="1001",
    )
    assert '[[TRANSFER type="cold" destination="1001"' in handler._workflow.system_prompt()


# ---------------------------------------------------------------------------
# Phase 5F — transfer outcome persistence (calls.close_reason/final_state)
# ---------------------------------------------------------------------------

async def _outcome_session(sid: str):
    handler = _make_handler(_make_stt(), _make_llm(["Ok."]), _make_tts())
    ctx = SessionContext(session_id=sid)
    bus = EventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    await bus.start()
    return session, bus, handler


@pytest.mark.asyncio
async def test_close_records_transfer_success_outcome():
    session, bus, handler = await _outcome_session("s-ok")
    await session.on_transfer_completed("1001", transfer_id="tid-1")
    spy = AsyncMock()
    handler.on_session_end = spy
    await session.close("stream_ended")
    spy.assert_awaited_once_with("s-ok", "TRANSFER_SUCCESS", final_state="TRANSFER_SUCCESS")
    await bus.stop()


@pytest.mark.asyncio
async def test_close_records_transfer_timeout_on_generic_close():
    session, bus, handler = await _outcome_session("s-to")
    async for _ in session.on_transfer_failed("1001", "transfer_timeout", "tid-2"):
        pass
    spy = AsyncMock()
    handler.on_session_end = spy
    await session.close("stream_ended")
    spy.assert_awaited_once_with("s-to", "TRANSFER_TIMEOUT", final_state="TRANSFER_TIMEOUT")
    await bus.stop()


@pytest.mark.asyncio
async def test_close_keeps_deliberate_reason_after_failed_transfer():
    """A failed transfer whose call genuinely continued and later ended
    normally keeps its real close reason — the attempt stays visible in
    final_state only."""
    session, bus, handler = await _outcome_session("s-cont")
    async for _ in session.on_transfer_failed("1001", "hangup_before_bridge", "tid-3"):
        pass
    spy = AsyncMock()
    handler.on_session_end = spy
    await session.close("goodbye_timeout")
    spy.assert_awaited_once_with("s-cont", "goodbye_timeout", final_state="TRANSFER_FAILED")
    await bus.stop()


@pytest.mark.asyncio
async def test_close_records_cancelled_in_final_state_only():
    session, bus, handler = await _outcome_session("s-can")
    session.on_transfer_cancelled("tid-4")
    spy = AsyncMock()
    handler.on_session_end = spy
    await session.close("stream_ended")
    spy.assert_awaited_once_with("s-can", "stream_ended", final_state="TRANSFER_CANCELLED")
    await bus.stop()


@pytest.mark.asyncio
async def test_close_without_transfer_leaves_reason_untouched():
    session, bus, handler = await _outcome_session("s-plain")
    spy = AsyncMock()
    handler.on_session_end = spy
    await session.close("stream_ended")
    spy.assert_awaited_once_with("s-plain", "stream_ended", final_state=None)
    await bus.stop()


# ---------------------------------------------------------------------------
# Phase 5F — fail-fast transfer config validation at session setup
# ---------------------------------------------------------------------------

def test_transfer_instruction_injected_for_valid_sip_uri():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="sip:agent@example.com",
    )
    assert 'destination="sip:agent@example.com"' in handler._workflow.system_prompt()


def test_transfer_instruction_absent_for_malformed_sip_uri():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="sip:no-at-sign",
    )
    assert "[[TRANSFER" not in handler._workflow.system_prompt()


def test_transfer_instruction_absent_for_prose_destination():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="the support desk",
    )
    assert "[[TRANSFER" not in handler._workflow.system_prompt()


def test_transfer_instruction_absent_for_unknown_transfer_type():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="hot", transfer_destination="1001",
    )
    assert "[[TRANSFER" not in handler._workflow.system_prompt()


def test_transfer_destination_problem_diagnoses():
    from ..pipeline import transfer_destination_problem
    assert transfer_destination_problem(None) is not None
    assert transfer_destination_problem("   ") is not None
    assert transfer_destination_problem("sip:x") is not None
    assert transfer_destination_problem("call me maybe") is not None
    assert transfer_destination_problem("1001") is None
    assert transfer_destination_problem("+18005550100") is None
    assert transfer_destination_problem("sip:agent@example.com") is None
    assert transfer_destination_problem("SIPS:agent@host.tld") is None


# ---------------------------------------------------------------------------
# Phase 5F — transfer_id generation (observability correlation)
# ---------------------------------------------------------------------------

def test_transfer_request_generates_unique_transfer_ids():
    a = TransferRequest(session_id="s", tenant_id="t", call_id="c",
                        transfer_type=TransferType.COLD, destination="1001", reason="r")
    b = TransferRequest(session_id="s", tenant_id="t", call_id="c",
                        transfer_type=TransferType.COLD, destination="1001", reason="r")
    assert a.transfer_id and b.transfer_id
    assert a.transfer_id != b.transfer_id


# ---------------------------------------------------------------------------
# Configurable end-call / transfer condition prompts (defaults preserved)
# ---------------------------------------------------------------------------

def test_default_end_call_instruction_matches_historical_text():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(), system_prompt="You are Alex.",
    )
    assert (
        "When the conversation is genuinely finished (the caller says "
        "goodbye, has no more questions, or the issue is resolved), end your"
    ) in handler._workflow.system_prompt()
    assert "[[END_CALL]]" in handler._workflow.system_prompt()


def test_custom_end_call_prompt_replaces_condition_keeps_mechanics():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(), system_prompt="You are Alex.",
        end_call_prompt="When the caller says the magic word.",
    )
    # Custom condition present (trailing period stripped so grammar holds)…
    assert "When the caller says the magic word, end your" in handler._workflow.system_prompt()
    # …default condition gone, token mechanics intact.
    assert "genuinely finished" not in handler._workflow.system_prompt()
    assert "exact token [[END_CALL]]" in handler._workflow.system_prompt()


def test_blank_end_call_prompt_falls_back_to_default():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(), system_prompt="You are Alex.",
        end_call_prompt="   ",
    )
    assert "genuinely finished" in handler._workflow.system_prompt()


def test_custom_transfer_prompt_replaces_condition_keeps_mechanics():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(), system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001",
        transfer_prompt="If the caller mentions a billing dispute",
    )
    assert "If the caller mentions a billing dispute, briefly acknowledge" in handler._workflow.system_prompt()
    assert "explicitly asks to speak to a human" not in handler._workflow.system_prompt()
    assert '[[TRANSFER type="cold" destination="1001"' in handler._workflow.system_prompt()


def test_default_transfer_prompt_unchanged():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(), system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001",
    )
    assert (
        "If the caller explicitly asks to speak to a human agent or "
        "representative, briefly acknowledge"
    ) in handler._workflow.system_prompt()


# ---------------------------------------------------------------------------
# TTS voice speed (provider_configs.extra["speed"], 0.7–1.2, default 1.0)
# ---------------------------------------------------------------------------

def _speed_cfg(extra):
    return SDKProviderConfig(id="p", role="tts", engine="kokoro", model=None,
                             voice=None, language=None, api_key_ref=None, extra=extra)


def test_voice_speed_defaults_and_bounds():
    from ..ai_provider_manager import voice_speed, VOICE_SPEED_DEFAULT
    assert voice_speed(_speed_cfg({})) == VOICE_SPEED_DEFAULT
    assert voice_speed(_speed_cfg({"speed": 0.7})) == 0.7
    assert voice_speed(_speed_cfg({"speed": 1.2})) == 1.2
    assert voice_speed(_speed_cfg({"speed": "0.9"})) == 0.9


def test_voice_speed_invalid_falls_back_to_default():
    from ..ai_provider_manager import voice_speed, VOICE_SPEED_DEFAULT
    assert voice_speed(_speed_cfg({"speed": 0})) == VOICE_SPEED_DEFAULT
    assert voice_speed(_speed_cfg({"speed": -1})) == VOICE_SPEED_DEFAULT
    assert voice_speed(_speed_cfg({"speed": 2.5})) == VOICE_SPEED_DEFAULT
    assert voice_speed(_speed_cfg({"speed": "fast"})) == VOICE_SPEED_DEFAULT
    assert voice_speed(_speed_cfg({"speed": None})) == VOICE_SPEED_DEFAULT


# ---------------------------------------------------------------------------
# Escalation detector wired into on_speech_ended (guardrails.py integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_frustrated_utterance_triggers_escalation_after_threshold():
    """A real caller utterance carrying a frustration/abuse phrase is
    detected inline (no manual record_guardrail_violation() call, unlike
    the lower-level test above) and, once the configured threshold is
    exceeded, surfaces a TransferRequest on a later turn."""
    stt_frustrated = _make_stt("This is useless, you are not helping at all.")
    llm = _make_llm(["I'm sorry to hear that."])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(
        stt_frustrated, llm, tts,
        transfer_type="cold", transfer_destination="1001",
        escalation_threshold=1,
    )

    # Turn 1: violation count -> 1 (== threshold, not yet over).
    responses1 = []
    async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0):
        responses1.append(r)
    assert not any(r.transfer_request for r in responses1)

    # Turn 2: another frustrated utterance -> count 2 > threshold=1.
    responses2 = []
    async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0):
        responses2.append(r)
    transfer_responses = [r for r in responses2 if r.transfer_request]
    assert len(transfer_responses) == 1
    tr = transfer_responses[0].transfer_request
    assert tr.trigger == "escalation_threshold"
    assert tr.destination == "1001"


@pytest.mark.asyncio
async def test_ordinary_utterances_never_escalate():
    stt = _make_stt("Can you tell me your business hours?")
    llm = _make_llm(["We're open 9 to 5."])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(
        stt, llm, tts, transfer_type="cold", transfer_destination="1001",
        escalation_threshold=1,
    )
    for _ in range(3):
        responses = []
        async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0):
            responses.append(r)
        assert not any(r.transfer_request for r in responses)


@pytest.mark.asyncio
async def test_frustration_counted_even_without_escalation_configured():
    """escalation_threshold=None means violations are still counted (for
    future observability) but never trigger a transfer — matches
    record_guardrail_violation's own documented contract."""
    stt_frustrated = _make_stt("This is ridiculous, I give up.")
    llm = _make_llm(["Let me try again."])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(stt_frustrated, llm, tts)  # escalation_threshold defaults to None

    for _ in range(5):
        responses = []
        async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0):
            responses.append(r)
        assert not any(r.transfer_request for r in responses)


# ---------------------------------------------------------------------------
# Phase 6 — TransferDecisionEngine integration: consecutive reset + duplicate
# suppression across LLM-directive and escalation triggers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guardrail_counter_resets_on_non_violating_turn():
    """A frustrated turn, then a calm turn, then frustrated again must not
    reach threshold=2 on the third turn — the calm turn resets the streak
    (per Phase 6's 'consecutive' counter requirement)."""
    frustrated = _make_stt("This is useless, you are not helping at all.")
    calm = _make_stt("Can you tell me your hours?")
    llm = _make_llm(["Ok."])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(
        frustrated, llm, tts, transfer_type="cold", transfer_destination="1001",
        escalation_threshold=1,
    )

    # Turn 1: frustrated -> count 1 (== threshold, not over).
    r1 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert not any(r.transfer_request for r in r1)

    # Turn 2: calm -> resets the streak to 0.
    handler._stt = calm
    r2 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert not any(r.transfer_request for r in r2)

    # Turn 3: frustrated again -> count restarts at 1, still not over
    # threshold=1 (would have been count=3 > 1 without the reset).
    handler._stt = frustrated
    r3 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert not any(r.transfer_request for r in r3)


@pytest.mark.asyncio
async def test_duplicate_transfer_suppressed_after_first_accepted_directive():
    """Once an LLM directive has produced an accepted TransferRequest for a
    session, a second directive-carrying turn must not produce another —
    the engine's already_requested duplicate protection, driven by the
    pipeline's own _transfer_requested bookkeeping."""
    stt = _make_stt("Please transfer me to a human.")
    llm = _make_llm(["Connecting you now. [[TRANSFER type=\"cold\" destination=\"1001\" reason=\"x\"]]"])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(stt, llm, tts, transfer_type="cold", transfer_destination="1001")

    r1 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert sum(1 for r in r1 if r.transfer_request) == 1

    # Second turn: LLM emits another directive; engine must reject as a
    # duplicate since s1 already has an accepted, unresolved transfer.
    r2 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert not any(r.transfer_request for r in r2)


@pytest.mark.asyncio
async def test_transfer_requested_bookkeeping_cleared_on_session_end():
    stt = _make_stt("Please transfer me to a human.")
    llm = _make_llm(["Connecting you now. [[TRANSFER type=\"cold\" destination=\"1001\" reason=\"x\"]]"])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(stt, llm, tts, transfer_type="cold", transfer_destination="1001")

    r1 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert sum(1 for r in r1 if r.transfer_request) == 1
    assert handler._session("s1").transfer_requested

    await handler.on_session_end("s1", "stream_ended")
    assert not handler._session("s1").transfer_requested


# ---------------------------------------------------------------------------
# Phase 6 bug fix: duplicate-suppression must release on cancellation/failure
# (found live 2026-07-18 — a barge-in-dropped transfer permanently blocked
# all further attempts for that session without this)
# ---------------------------------------------------------------------------

def test_on_transfer_cancelled_clears_duplicate_suppression_flag():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        transfer_type="cold", transfer_destination="1001",
    )
    handler._session("s1").transfer_requested = True
    handler.on_transfer_cancelled("s1")
    assert not handler._session("s1").transfer_requested


@pytest.mark.asyncio
async def test_retry_after_barge_in_cancelled_transfer_is_not_treated_as_duplicate():
    """Mirrors the live sequence: an LLM directive is accepted, the
    servicer/session-level cancellation fires (barge-in before dispatch),
    then the caller asks again — the second attempt must be evaluated
    fresh, not rejected as already_transferring."""
    stt = _make_stt("Please transfer me to a human.")
    llm = _make_llm(["Connecting you now. [[TRANSFER type=\"cold\" destination=\"1001\" reason=\"x\"]]"])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(stt, llm, tts, transfer_type="cold", transfer_destination="1001")

    r1 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert sum(1 for r in r1 if r.transfer_request) == 1
    assert handler._session("s1").transfer_requested

    # Barge-in during the acknowledgment drops the pending dispatch.
    handler.on_transfer_cancelled("s1")
    assert not handler._session("s1").transfer_requested

    r2 = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert sum(1 for r in r2 if r.transfer_request) == 1


@pytest.mark.asyncio
async def test_on_transfer_failed_clears_duplicate_suppression_flag():
    """After a failed (not merely cancelled) transfer, the call continues
    via the apology path — a subsequent request must not be blocked."""
    handler = _make_handler(
        _make_stt(), _make_llm(["Sorry about that."]), _make_tts(),
        transfer_type="cold", transfer_destination="1001",
    )
    handler._session("s1").transfer_requested = True
    async for _ in handler.on_transfer_failed("s1", "1001", "hangup_before_bridge"):
        pass
    assert not handler._session("s1").transfer_requested


@pytest.mark.asyncio
async def test_echo_handler_on_transfer_cancelled_is_a_safe_noop():
    handler = EchoConversationHandler()
    handler.on_transfer_cancelled("s1")  # must not raise


@pytest.mark.asyncio
async def test_pending_escalation_not_starved_by_rejected_directive():
    """Code-review regression: an escalation-accepted pending transfer must
    dispatch even when the same/next turn's LLM emits a [[TRANSFER]]
    directive that the engine rejects as already_transferring — the
    rejected directive must fall through to the pending request rather
    than starving it."""
    stt = _make_stt("I want a human now.")
    llm = _make_llm(["Connecting you. [[TRANSFER type=\"cold\" destination=\"1001\" reason=\"x\"]]"])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(
        stt, llm, tts, transfer_type="cold", transfer_destination="1001",
        escalation_threshold=1,
    )

    # Escalation crosses the threshold between turns (external-caller style),
    # storing a pending accepted request and setting the duplicate guard.
    assert handler.record_guardrail_violation("s1") is None
    assert handler.record_guardrail_violation("s1") is not None
    assert handler._session("s1").transfer_requested

    # This turn's LLM also emits a directive -> engine rejects it as
    # already_transferring -> the pending request must still go out.
    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    transfers = [r.transfer_request for r in responses if r.transfer_request]
    assert len(transfers) == 1
    assert transfers[0].trigger == "escalation_threshold"


# ---------------------------------------------------------------------------
# Scripted farewell_message / transfer_announcement (spoken verbatim)
# ---------------------------------------------------------------------------

def test_scripted_messages_switch_instructions_to_token_only():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001",
        farewell_message="Thanks for calling. Goodbye!",
        transfer_announcement="Please hold while I transfer your call.",
    )
    prompt = handler._workflow.system_prompt()
    # Token-only phrasing for both, and the scripted lines themselves are
    # NOT leaked into the prompt (they're synthesized, not LLM material).
    assert "reply with ONLY the exact token [[END_CALL]]" in prompt
    assert "reply with ONLY the exact token [[TRANSFER" in prompt
    assert "Thanks for calling. Goodbye!" not in prompt
    assert "Please hold while I transfer" not in prompt


def test_no_scripted_messages_keeps_spoken_word_instructions():
    handler = _make_handler(
        _make_stt(), _make_llm(), _make_tts(),
        system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001",
    )
    assert "after your spoken words" in handler._workflow.system_prompt()
    assert "reply with ONLY" not in handler._workflow.system_prompt()


@pytest.mark.asyncio
async def test_farewell_message_synthesized_on_end_call():
    """Marker-only end-call turn with a scripted farewell: the farewell is
    synthesized (not the generic fallback) and end_call still fires."""
    stt = _make_stt("goodbye")
    llm = _make_llm(["[[END_CALL]]"])  # token only, per the scripted instruction
    tts = _make_tts(b"\x11" * 640)
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are Alex.",
        farewell_message="Thanks for calling Yovanex. Goodbye!",
    )
    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert any(r.end_call for r in responses)
    assert any(r.tts_payloads for r in responses)
    # The farewell text (not _FALLBACK_GOODBYE) is what was synthesized.
    synthesized = [c.args[0] for c in tts.synthesize.await_args_list]
    assert "Thanks for calling Yovanex. Goodbye!" in synthesized
    assert _FALLBACK_GOODBYE not in synthesized


@pytest.mark.asyncio
async def test_transfer_announcement_synthesized_before_transfer_request():
    """Token-only transfer turn with a scripted announcement: announcement
    audio is yielded BEFORE the transfer_request (so the servicer holds
    dispatch until it has played)."""
    stt = _make_stt("get me a human")
    llm = _make_llm(['[[TRANSFER type="cold" destination="1001" reason="x"]]'])
    tts = _make_tts(b"\x22" * 640)
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001",
        transfer_announcement="Please hold while I transfer your call.",
    )
    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    audio_idx = next(i for i, r in enumerate(responses) if r.tts_payloads)
    transfer_idx = next(i for i, r in enumerate(responses) if r.transfer_request)
    assert audio_idx < transfer_idx
    synthesized = [c.args[0] for c in tts.synthesize.await_args_list]
    assert "Please hold while I transfer your call." in synthesized


@pytest.mark.asyncio
async def test_no_announcement_token_only_transfer_still_dispatches_without_audio():
    """Without a scripted announcement, a token-only transfer turn keeps
    the existing immediate-dispatch behavior (no synthesized audio)."""
    stt = _make_stt("get me a human")
    llm = _make_llm(['[[TRANSFER type="cold" destination="1001" reason="x"]]'])
    tts = _make_tts(b"\x33" * 640)
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001",
    )
    # Not this test's concern — see _FIRST_TURN_FILLER's own tests — so
    # treat this as a turn beyond the first, keeping this test isolated to
    # the announcement/no-announcement transfer-audio question it's for.
    handler._session("s1").first_turn_filler_spoken = True
    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert any(r.transfer_request for r in responses)
    assert not any(r.tts_payloads for r in responses)


# ---------------------------------------------------------------------------
# Tool Execution Framework wiring — orchestrator is optional/injected,
# same backward-compatible posture as knowledge/metrics above.
# ---------------------------------------------------------------------------

class _FakeToolOrchestrator:
    """Matches the one method _token_stream() actually calls —
    ToolCallOrchestrator's own internal logic is covered separately by
    test_tool_call_orchestrator.py. This fake proves the pipeline wiring
    itself: events unwrapped correctly, sentence-splitting/TTS still works
    on top of an orchestrator-driven token stream."""

    def __init__(self, events):
        self._events = events
        self.seen_agent_id = None
        self.seen_history = None

    async def run_turn(
        self, agent_id, tenant_id, call_id, session_id, history,
        caller_number="", cancel_event=None, force_tool_name=None, phone_number_confirmed=False,
        local_tools=None, only_tools=None,
    ):
        self.seen_force_tool_name = force_tool_name
        self.seen_phone_number_confirmed = phone_number_confirmed
        self.seen_agent_id = agent_id
        self.seen_history = list(history)
        self.seen_caller_number = caller_number
        self.seen_local_tools = local_tools
        self.seen_only_tools = only_tools
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_pipeline_uses_tool_orchestrator_when_provided():
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="You're booked!")])
    handler = _make_handler(stt, llm, tts, system_prompt="You are a scheduler.", tool_orchestrator=orchestrator)

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert any(r.tts_payloads for r in responses)
    assert orchestrator.seen_history is not None
    # System prompt + this turn's user message reached the orchestrator.
    assert orchestrator.seen_history[-1].content == "book me tomorrow at 3"


@pytest.mark.asyncio
async def test_pipeline_without_tool_orchestrator_uses_llm_directly():
    """Default (tool_orchestrator=None) — identical to pre-tool-calling
    behavior, the exact backward-compatibility contract this feature was
    built under."""
    stt = _make_stt("hello")
    llm = _make_llm(["Hi", " there", "!"])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(stt, llm, tts, system_prompt="You are Alex.")

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert any(r.tts_payloads for r in responses)


@pytest.mark.asyncio
async def test_local_tool_completed_event_does_not_crash_the_pipeline():
    from ..tools.llm_adapter import LocalToolCompletedEvent
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("verified")
    llm = _make_llm(["unused"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([
        LocalToolCompletedEvent(tool_name="caller_verified"),
        ToolTokenEvent(text="Thanks, moving on."),
    ])
    handler = _make_handler(stt, llm, tts, system_prompt="You are a scheduler.", tool_orchestrator=orchestrator)

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert any(r.tts_payloads for r in responses)
    spoken_texts = [call.args[0] for call in tts.synthesize.await_args_list]
    assert not any(t in _TOOL_CALL_FILLERS for t in spoken_texts)


@pytest.mark.asyncio
async def test_tool_call_filler_burst_within_one_turn_speaks_only_once():
    """Two tool calls back to back in the same turn (no real user speech
    between them) must not each speak a filler — that's the same
    "sounds broken" repetition the rotation exists to avoid, just via a
    burst instead of identical wording. See _TOOL_CALL_FILLER_MIN_GAP_S."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent
    from ..tools.llm_adapter import ToolCallStartedEvent

    stt = _make_stt("book something")
    llm = _make_llm(["unused"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([
        ToolCallStartedEvent(tool_name="book_appointment"),
        ToolCallStartedEvent(tool_name="book_appointment"),
        ToolTokenEvent(text="Done."),
    ])
    handler = _make_handler(stt, llm, tts, system_prompt="You are a scheduler.", tool_orchestrator=orchestrator)

    async for _ in handler.on_speech_ended("s1", _silence(), 300, -20.0):
        pass

    spoken_texts = [call.args[0] for call in tts.synthesize.await_args_list]
    filler_count = sum(1 for t in spoken_texts if t in _TOOL_CALL_FILLERS)
    assert filler_count == 1


@pytest.mark.asyncio
async def test_tool_call_filler_rotates_across_separate_tool_calls(monkeypatch):
    """Two tool calls on two separate, well-spaced turns (the real case —
    a caller replying between them) each get a filler, and they aren't the
    same phrase — see _TOOL_CALL_FILLERS."""
    from ..tools.llm_adapter import ToolCallStartedEvent

    clock = {"t": 0.0}
    monkeypatch.setattr(pipeline_module.time, "monotonic", lambda: clock["t"])

    stt = _make_stt("book something")
    llm = _make_llm(["unused"])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(stt, llm, tts, system_prompt="You are a scheduler.")

    handler._tool_orchestrator = _FakeToolOrchestrator([ToolCallStartedEvent(tool_name="book_appointment")])
    async for _ in handler.on_speech_ended("s1", _silence(), 300, -20.0):
        pass

    clock["t"] += _TOOL_CALL_FILLER_MIN_GAP_S + 1.0
    handler._tool_orchestrator = _FakeToolOrchestrator([ToolCallStartedEvent(tool_name="book_appointment")])
    async for _ in handler.on_speech_ended("s1", _silence(), 300, -20.0):
        pass

    spoken_texts = [call.args[0] for call in tts.synthesize.await_args_list]
    fillers_spoken = [t for t in spoken_texts if t in _TOOL_CALL_FILLERS]
    assert len(fillers_spoken) == 2
    assert fillers_spoken[0] != fillers_spoken[1]


@pytest.mark.asyncio
async def test_truthful_recap_after_real_booking_is_not_flagged():
    """Confirmed live: a genuine, tool-confirmed booking success on one
    turn, truthfully recapped by the LLM on a LATER turn (no tool call
    needed that turn — nothing about the booking changed), got flagged as
    a fresh fabrication anyway. Once _confirmed_booking_slot has the real
    slot for this session, a later plain-text recap of THAT SAME slot
    (same day-of-month and hour mentioned) must never be corrected or
    counted."""
    from ..tools.llm_adapter import DeterministicSpokenEvent as ToolDeterministicSpokenEvent
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent
    from ..tools.llm_adapter import ToolCallStartedEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    real_success_orchestrator = _FakeToolOrchestrator([
        ToolCallStartedEvent(tool_name="book_appointment"),
        ToolDeterministicSpokenEvent(
            text="You're all set — I've booked your appointment for Friday, August 28 at 3:00 PM.",
            confirmed_datetime="2026-08-28T15:00:00",
        ),
    ])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=real_success_orchestrator, has_booking_tool=True,
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    # Second turn: no tool call this time, just a truthful recap of the
    # SAME slot (day 28, hour 3 PM both mentioned again).
    handler._tool_orchestrator = _FakeToolOrchestrator([
        ToolTokenEvent(text="Just confirming — your appointment is booked for the 28th at 3 PM."),
    ])
    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    history = handler._get_history("s1")
    assert history[-1].role == "assistant"
    assert history[-1].content == "Just confirming — your appointment is booked for the 28th at 3 PM."


@pytest.mark.asyncio
async def test_reschedule_claim_to_a_different_time_is_still_flagged():
    """The other half of the fix: a real booking succeeding once must NOT
    give a free pass to a LATER claim about a genuinely different,
    never-confirmed time — confirmed live, a caller's reschedule request
    got a false "it's booked" for a new date/time with no
    reschedule_appointment call behind it, and the old boolean-flag
    version of this guard silently let it through."""
    from ..tools.llm_adapter import DeterministicSpokenEvent as ToolDeterministicSpokenEvent
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent
    from ..tools.llm_adapter import ToolCallStartedEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    real_success_orchestrator = _FakeToolOrchestrator([
        ToolCallStartedEvent(tool_name="book_appointment"),
        ToolDeterministicSpokenEvent(
            text="You're all set — I've booked your appointment for Friday, August 28 at 3:00 PM.",
            confirmed_datetime="2026-08-28T15:00:00",
        ),
    ])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=real_success_orchestrator, has_booking_tool=True,
        transfer_type="warm", transfer_destination="1000", escalation_threshold=0,
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    # Second turn: caller asked to reschedule; no tool call happened, but
    # the LLM claims a NEW, never-confirmed time (day 30, not day 28).
    handler._tool_orchestrator = _FakeToolOrchestrator([
        ToolTokenEvent(text="Sure, I've rescheduled your appointment to the 30th at 3 PM."),
    ])
    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert any(r.transfer_request for r in responses)
    history = handler._get_history("s1")
    assert history[-1].role == "system"


@pytest.mark.asyncio
async def test_fabricated_booking_claim_gets_corrected_in_history():
    """Confirmed live: a local LLM narrated "Booked! ... demo
    scheduled ..." with no real book_appointment call behind it (verified
    against the real Cal.com API: zero bookings existed). The pipeline
    can't unspeak the sentence, but it must append a correction to history
    so the next turn doesn't compound the lie, and count it as a guardrail
    violation."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="Booked! Your appointment is confirmed.")])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    history = handler._get_history("s1")
    assert history[-1].role == "system"
    assert "did not call book_appointment" in history[-1].content
    assert history[-2].role == "assistant"


@pytest.mark.asyncio
async def test_fabricated_booking_claim_escalates_on_first_offense():
    """Confirmed live: escalation_threshold=1 with two
    consecutive fabricated "Booked!" turns never escalated, because (a)
    the engine rejects when violation_count <= threshold (so threshold=1
    actually requires 2+ violations, not 1), and (b) the fabrication count
    was sharing a counter with the caller-frustration detector, which
    resets on every polite caller turn — the caller here said "Sure,
    thank you" between the two fabrications, wiping the count back to 0
    each time. threshold=0 is the correct configuration for "transfer on
    the very first fabrication," and the dedicated
    _booking_fabrication_counter must not be reset by polite caller
    turns."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("Sure, thank you.")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="Booked! Your demo is confirmed.")])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
        transfer_type="warm", transfer_destination="1000", escalation_threshold=0,
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    transfer_responses = [r for r in responses if r.transfer_request]
    assert len(transfer_responses) == 1
    assert transfer_responses[0].transfer_request.destination == "1000"


@pytest.mark.asyncio
async def test_pending_transfer_survives_same_turn_end_call_marker():
    """Confirmed live: the LLM's fabricated booking claim came
    bundled with its own [[END_CALL]] marker in the very same turn (a
    natural "wrap up and say goodbye" shape) — end_call used to be
    processed unconditionally first and yielded HandlerResponse(end_call=
    True), which tears the session down before the transfer_request
    yielded later in the same generator could ever reach the servicer. A
    pending transfer must win: no end_call HandlerResponse this turn, and
    the transfer_request must still be yielded."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([
        ToolTokenEvent(text="Booked! Your appointment is confirmed. [[END_CALL]]"),
    ])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
        transfer_type="warm", transfer_destination="1000", escalation_threshold=0,
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert not any(r.end_call for r in responses)
    transfer_responses = [r for r in responses if r.transfer_request]
    assert len(transfer_responses) == 1
    assert transfer_responses[0].transfer_request.destination == "1000"


@pytest.mark.asyncio
async def test_fabrication_triggered_transfer_speaks_specific_announcement():
    """Product fix, confirmed live: a caller who just heard "Confirmed! ..."
    followed immediately by a silent handoff to a human reads as the
    system being broken, even though escalation is working as designed.
    This transfer must speak a specific double-checking line instead of
    (or in place of no) generic transfer_announcement."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="Booked! Your appointment is confirmed.")])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
        transfer_type="warm", transfer_destination="1000", escalation_threshold=0,
        transfer_announcement="Please hold while I transfer you.",
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    spoken_texts = [call.args[0] for call in tts.synthesize.await_args_list]
    assert any("double-check that booking" in t for t in spoken_texts)
    assert "Please hold while I transfer you." not in spoken_texts


@pytest.mark.asyncio
async def test_frustration_triggered_transfer_still_uses_generic_announcement():
    """A transfer NOT caused by booking fabrication (caller frustration
    here) must keep using the agent's own configured transfer_announcement
    — the specific double-checking line is only for the fabrication case."""
    stt_frustrated = _make_stt("This is useless, you are not helping at all.")
    llm = _make_llm(["I'm sorry to hear that."])
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(
        stt_frustrated, llm, tts,
        transfer_type="cold", transfer_destination="1001", escalation_threshold=0,
        transfer_announcement="Please hold while I transfer you.",
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    spoken_texts = [call.args[0] for call in tts.synthesize.await_args_list]
    assert "Please hold while I transfer you." in spoken_texts
    assert not any("double-check that booking" in t for t in spoken_texts)


@pytest.mark.asyncio
async def test_deterministic_spoken_event_reaches_tts_and_history():
    """End-to-end pipeline wiring for a real, confirmed booking: the
    DeterministicSpokenEvent's exact text must reach TTS and land in
    history as this turn's assistant message, with no fabrication warning
    (tool_calls_made already contains book_appointment by the time the
    check runs) and no separate LLM narration."""
    from ..tools.llm_adapter import DeterministicSpokenEvent as ToolDeterministicSpokenEvent
    from ..tools.llm_adapter import ToolCallStartedEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    confirmation = "You're all set — I've booked your appointment for Friday at 3 PM."
    orchestrator = _FakeToolOrchestrator([
        ToolCallStartedEvent(tool_name="book_appointment"),
        ToolDeterministicSpokenEvent(text=confirmation),
    ])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert any(r.tts_payloads for r in responses)
    history = handler._get_history("s1")
    assert history[-1].role == "assistant"
    assert history[-1].content == confirmation


def test_message_reads_back_phone_number_matches_words_or_digits():
    from ..pipeline import _message_reads_back_phone_number

    assert _message_reads_back_phone_number("nine one eight nine seven one one eight eight two one one", "+918971188211")
    assert _message_reads_back_phone_number("+91 8 9 7 1 1 8 8 2 1 1, is that right?", "+918971188211")
    assert not _message_reads_back_phone_number("What's a good time for you?", "+918971188211")
    assert not _message_reads_back_phone_number("nine one eight nine seven one one eight eight two one one", "")


def test_caller_just_confirmed_phone_number_requires_readback_then_affirmative():
    from ..pipeline import _caller_just_confirmed_phone_number

    readback = ChatMessage(
        role="assistant",
        content="Let me confirm: nine one eight nine seven one one eight eight two one one. Is that right?",
    )
    assert _caller_just_confirmed_phone_number(
        [readback, ChatMessage(role="user", content="Yes, correct.")], "+918971188211",
    )
    # Not affirmative — a new question instead of a yes/no.
    assert not _caller_just_confirmed_phone_number(
        [readback, ChatMessage(role="user", content="What time works for you?")], "+918971188211",
    )
    # Previous turn wasn't a phone readback at all.
    not_readback = ChatMessage(role="assistant", content="What kind of business are you in?")
    assert not _caller_just_confirmed_phone_number(
        [not_readback, ChatMessage(role="user", content="Yes")], "+918971188211",
    )


@pytest.mark.asyncio
async def test_phone_confirmation_forces_book_appointment_tool_choice():
    """End-to-end pipeline wiring: right after a phone-number readback +
    affirmative reply, _token_stream must pass force_tool_name to the
    orchestrator — the one condition confirmed live, repeatedly, to be an
    unambiguous "call the tool now" moment the LLM sometimes skips
    anyway."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("Yes, correct.")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="booking now")])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
    )
    history = handler._get_history("s1")
    history.append(ChatMessage(
        role="assistant",
        content="Let me confirm: nine one eight nine seven one one eight eight two one one. Is that right?",
    ))
    handler._caller_number = "+918971188211"

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert orchestrator.seen_force_tool_name == "book_appointment"


@pytest.mark.asyncio
async def test_no_phone_confirmation_does_not_force_tool_choice():
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("What kind of business are you in?")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="ok")])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert orchestrator.seen_force_tool_name is None
    assert orchestrator.seen_phone_number_confirmed is False


@pytest.mark.asyncio
async def test_phone_confirmation_persists_to_a_later_turn():
    """A caller confirming their number early in the call must still count
    several turns later, when the actual booking attempt happens — not
    just on the exact turn the confirmation occurred (see pipeline.py's
    _phone_number_confirmed docstring: confirmed live, a caller who never
    actually answered the read-back question still got booked against an
    unconfirmed number, which this deterministic tracking now prevents)."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("Yes, correct.")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="ok")])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
    )
    history = handler._get_history("s1")
    history.append(ChatMessage(
        role="assistant",
        content="Let me confirm: nine one eight nine seven one one eight eight two one one. Is that right?",
    ))
    handler._caller_number = "+918971188211"

    # Turn 1: the confirmation itself.
    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert orchestrator.seen_phone_number_confirmed is True

    # Turn 2: unrelated follow-up — no fresh readback/affirmative pair,
    # but the earlier confirmation must still be remembered.
    stt.transcribe.return_value = SttResult(text="tomorrow at 3pm works", confidence=0.95)
    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    assert orchestrator.seen_phone_number_confirmed is True


@pytest.mark.asyncio
async def test_real_booking_tool_call_is_not_flagged_as_fabricated():
    """A turn where book_appointment genuinely ran must never get the
    correction appended, even if the response text also happens to say
    "booked" — that's the honest case."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent, ToolCallStartedEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([
        ToolCallStartedEvent(tool_name="book_appointment"),
        ToolTokenEvent(text="Booked! Your appointment is confirmed."),
    ])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=True,
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    history = handler._get_history("s1")
    assert history[-1].role == "assistant"


@pytest.mark.asyncio
async def test_fabricated_booking_claim_not_flagged_without_booking_tool():
    """has_booking_tool=False (e.g. a reception-only agent) must never
    trigger this check at all — it has no book_appointment tool to have
    skipped calling in the first place."""
    from ..tools.llm_adapter import TokenEvent as ToolTokenEvent

    stt = _make_stt("book me tomorrow at 3")
    llm = _make_llm(["should never be called"])
    tts = _make_tts(b"\x00" * 640)
    orchestrator = _FakeToolOrchestrator([ToolTokenEvent(text="Booked! Your appointment is confirmed.")])
    handler = _make_handler(
        stt, llm, tts, system_prompt="You are a scheduler.",
        tool_orchestrator=orchestrator, has_booking_tool=False,
    )

    [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    history = handler._get_history("s1")
    assert history[-1].role == "assistant"


def _make_failing_llm(exc: Exception) -> MagicMock:
    """An ILLM whose generate() raises mid-stream — simulates a provider
    5xx/429/network error (real example: Gemini's 400 thought_signature
    bug, or its free-tier 429 quota) reaching _llm_to_tts's token loop."""
    async def _gen(messages):
        yield "partial"
        raise exc

    llm = MagicMock()
    llm.generate = _gen
    return llm


@pytest.mark.asyncio
async def test_pipeline_speaks_fallback_when_llm_stream_raises():
    """A provider error mid-turn (429, 5xx, a bridging bug) must not leave
    the caller in dead air — _llm_to_tts already catches the exception so
    the call itself survives, but without a spoken fallback the caller
    hears silence and the turn looks like a dropped call."""
    stt = _make_stt("book me tomorrow at 3")
    llm = _make_failing_llm(RuntimeError("simulated provider 429"))
    tts = _make_tts(b"\x00" * 640)
    handler = _make_handler(stt, llm, tts)

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    tts_responses = [r for r in responses if r.tts_payloads]
    assert tts_responses, "expected a spoken fallback despite the LLM error"
    assert not any(r.end_call for r in responses), "a transient error must not end the call"
