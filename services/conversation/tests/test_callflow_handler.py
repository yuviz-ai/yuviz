"""
CallFlowConversationHandler — audio, timers, the out-of-band egress queue,
and delegation. Fake ITTS with a fixed-size PCM output keeps prompt
"duration" negligible and predictable, so short real `timeout_ms` values
drive these tests without a separate fake-clock abstraction.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import pytest
from libs.config_sdk import Agent, ConversationInfo, MediaInfo, Policies
from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs, RuntimeConfig, Tenant
from libs.config_sdk.callflow import parse_graph
from libs.config_sdk.workflow import starter_graph

from ..callflow.handler import CallFlowConversationHandler
from ..callflow.runner import CallFlowRunner
from ..pipeline import PipelineConversationHandler
from ..provider_bundle import ProviderBundle
from .test_pipeline import _make_llm, _make_stt, _make_tts


class FakeTTS:
    """One fixed 2-byte PCM sample per synthesize() call regardless of
    text — makes every prompt's "duration" ~0s, so a `timeout_ms` of a few
    tens of ms is all a Listen timer needs to fire promptly in real time."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        self.calls.append(text)
        return b"\x00\x00"


def _menu_timeout_graph() -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "menu", "type": "menu",
             "data": {"name": "menu", "prompt": "Press 1.", "timeout_ms": 20, "max_retries": 1}},
            {"id": "one", "type": "hangup", "data": {"name": "one", "prompt": "One."}},
            {"id": "gone", "type": "hangup", "data": {"name": "gone", "prompt": ""}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "menu"},
            {"id": "e1", "source": "menu", "target": "one", "data": {"key": "1"}},
            {"id": "e2", "source": "menu", "target": "gone", "data": {"key": "timeout"}},
        ],
    }


def _collect_to_agent_graph(*, sensitive: bool) -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "collect", "type": "collect",
             "data": {"name": "collect", "prompt": "Enter your PIN.", "timeout_ms": 3000,
                      "max_retries": 1, "variable": "pin", "min_digits": 4, "max_digits": 4,
                      "terminator": "#", "sensitive": sensitive}},
            {"id": "a", "type": "agent", "data": {"name": "a", "agent_id": "agent-uuid-1"}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "collect"},
            {"id": "e1", "source": "collect", "target": "a"},
        ],
    }


def _dial_graph() -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "d", "type": "dial",
             "data": {"name": "d", "prompt": "Connecting.", "destination": "+15551230000"}},
        ],
        "edges": [{"id": "e0", "source": "start", "target": "d"}],
    }


def _race_graph() -> dict:
    """menu1 --digit '1'--> menu2 --timeout--> boom. menu2 is also a `menu`
    (not a terminal type), so a *stale* timeout armed for menu1 — delivered
    after a digit has already moved the runner to menu2 — would, if not
    dropped by node id, still find a menu node willing to act on it and take
    menu2's own timeout edge: a real double transition, not one masked by a
    terminal node's on_timeout()/on_digit() no-op."""
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "menu1", "type": "menu",
             "data": {"name": "menu1", "prompt": "Press 1.", "timeout_ms": 5000, "max_retries": 1}},
            {"id": "menu2", "type": "menu",
             "data": {"name": "menu2", "prompt": "Press 2.", "timeout_ms": 5000, "max_retries": 1}},
            {"id": "boom", "type": "hangup", "data": {"name": "boom", "prompt": "Boom."}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "menu1"},
            {"id": "e1", "source": "menu1", "target": "menu2", "data": {"key": "1"}},
            {"id": "e2", "source": "menu2", "target": "boom", "data": {"key": "timeout"}},
            {"id": "e3", "source": "menu2", "target": "boom", "data": {"key": "9"}},
        ],
    }


def _no_prompt_hangup_graph() -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "h", "type": "hangup", "data": {"name": "h", "prompt": ""}},
        ],
        "edges": [{"id": "e0", "source": "start", "target": "h"}],
    }


async def _no_handoff(agent_slug: str, seeded_variables: dict):
    return None


async def _no_voice(tts_config_id: str):
    return None


def _handler(graph_raw: dict, *, handoff=None, variables=None):
    runner = CallFlowRunner(parse_graph(graph_raw), tts_config_id=None, variables=variables)
    tts = FakeTTS()
    handler = CallFlowConversationHandler(
        runner, tts=tts, sample_rate=8000, session_id="s1", tenant_id="t1", call_id="c1",
        handoff=handoff or _no_handoff, voice_for=_no_voice,
        agent_slugs={"agent-uuid-1": "support-bot"},
    )
    return handler, runner, tts


def _delegate_factory(captured_variables: dict):
    """Real PipelineConversationHandler as the handoff target — captures
    the seeded variables it was actually constructed with, so the redaction
    assertions are against the delegate's own WorkflowRunner, not a stub."""
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        id="t1", slug="test", name="Test", region="us",
        vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
        no_speech_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
        transfer_timeout_ms=None,
        default_stt_config_id=None, default_llm_config_id=None, default_tts_config_id=None,
        config_version=1, updated_at=now,
    )
    workflow = starter_graph("Hi there.", "")
    agent = Agent(
        id="a1", slug="support-bot", tenant_id="t1", name="Support",
        greeting="Hi there.", system_prompt="", goodbye_grace_ms=0,
        stt_config_id=None, llm_config_id=None, tts_config_id=None,
        status="active", config_version=1, updated_at=now, workflow=workflow,
    )
    placeholder = SDKProviderConfig(
        id="p1", role="stt", engine="fake", model=None, voice=None, language=None, api_key_ref=None,
    )
    runtime_config = RuntimeConfig(
        tenant=tenant, agent=agent,
        providers=ProviderConfigs(stt=placeholder, llm=placeholder, tts=placeholder),
        conversation=ConversationInfo(
            greeting="Hi there.", system_prompt="", workflow=workflow, workflow_draft=workflow,
        ),
        media=MediaInfo(voice=None, language=None),
        policies=Policies(
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            silence_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None, goodbye_grace_ms=0,
        ),
        tools=[], version=1, resolved_at=now,
    )
    bundle = ProviderBundle(stt=_make_stt(), llm=_make_llm(), tts=_make_tts())

    async def handoff(agent_slug: str, seeded_variables: dict) -> PipelineConversationHandler:
        captured_variables.update(seeded_variables)
        return PipelineConversationHandler(runtime_config, bundle, initial_variables=seeded_variables)

    return handoff


async def _drain(handler: CallFlowConversationHandler) -> list:
    responses = []
    while not handler.out_responses.empty():
        responses.append(handler.out_responses.get_nowait())
    return responses


# ── Test 12: digit -> agent handoff streams the target's greeting ──────────

@pytest.mark.asyncio
async def test_menu_digit_handoff_streams_greeting_with_seeded_variables():
    captured: dict = {}
    handler, runner, tts = _handler(
        _collect_to_agent_graph(sensitive=False), handoff=_delegate_factory(captured),
    )
    await asyncio.sleep(0)  # let open() run: Speak(collect prompt) + Listen

    for d in "1234":
        await handler.on_dtmf("s1", d)
    await asyncio.sleep(0.05)

    assert captured.get("pin") == "1234"
    responses = await _drain(handler)
    assert any(r.tts_payloads for r in responses[:-1])   # the collect prompt
    assert responses[-1].tts_payloads                    # the delegate's greeting
    assert handler._delegate is not None
    assert handler._delegate._workflow.variables.get("pin") == "1234"


# ── Test 13: menu timeout out-of-band; hangup-with-no-prompt shape ─────────

@pytest.mark.asyncio
async def test_menu_timeout_with_no_inbound_message_emits_out_of_band_response():
    handler, runner, tts = _handler(_menu_timeout_graph())
    response = await asyncio.wait_for(handler.out_responses.get(), timeout=1)  # the menu prompt
    assert response.tts_payloads
    timeout_response = await asyncio.wait_for(handler.out_responses.get(), timeout=1)
    assert timeout_response.tts_payloads == []  # "gone" hangup has no prompt
    assert timeout_response.end_call is True
    assert runner.node.id == "gone"


@pytest.mark.asyncio
async def test_hangup_with_no_prompt_response_has_empty_payload_and_end_call():
    handler, runner, tts = _handler(_no_prompt_hangup_graph())
    response = await asyncio.wait_for(handler.out_responses.get(), timeout=1)
    assert response.tts_payloads == []
    assert response.end_call is True


# ── Test 14: dial + mid-node exception ──────────────────────────────────────

@pytest.mark.asyncio
async def test_dial_node_emits_cold_transfer_request():
    handler, runner, tts = _handler(_dial_graph())
    response = await asyncio.wait_for(handler.out_responses.get(), timeout=1)
    assert response.tts_payloads == [b"\x00\x00"]  # "Connecting." spoken first
    assert response.transfer_request is not None
    assert response.transfer_request.destination == "+15551230000"


@pytest.mark.asyncio
async def test_handler_exception_mid_node_yields_clean_end_call():
    handler, runner, tts = _handler(_menu_timeout_graph())
    await asyncio.wait_for(handler.out_responses.get(), timeout=1)  # drain the menu prompt

    async def _boom(text, sample_rate):
        raise RuntimeError("tts provider unreachable")

    tts.synthesize = _boom
    await handler.on_dtmf("s1", "1")  # "one" hangup has a prompt -> synthesize raises
    response = await asyncio.wait_for(handler.out_responses.get(), timeout=1)
    assert response.end_call is True
    assert not handler._driver_task.cancelled()  # ended cleanly, not by cancellation


# ── Test 14a: digit + already-stale timeout in the same tick ───────────────

@pytest.mark.asyncio
async def test_digit_and_stale_timeout_same_tick_yield_exactly_one_transition():
    handler, runner, tts = _handler(_race_graph())
    await asyncio.wait_for(handler.out_responses.get(), timeout=1)  # drain menu1's prompt

    assert runner.node.id == "menu1"
    stale_generation = handler._listen_generation  # the arm behind menu1's prompt
    # Both enqueued before the driver task gets to run either (neither
    # on_dtmf() nor this put_nowait() suspends) — a real timer armed for
    # menu1 firing at the same instant a keypress moves the runner off
    # menu1, not a call into on_timeout()/on_digit() directly (which
    # "cannot fail" per the plan). The digit is queued first so it is
    # processed first, moving the runner to menu2 — a *different* menu
    # node that would happily act on an unqualified on_timeout() call — by
    # the time the stale menu1 timeout is dequeued behind it.
    await handler.on_dtmf("s1", "1")
    handler._events.put_nowait(("timeout", stale_generation))
    await asyncio.sleep(0.05)

    # Exactly one transition (menu1 -> menu2, via the digit) — the stale
    # timeout for menu1 must not also fire menu2's own timeout edge to boom.
    assert runner.node.id == "menu2"
    assert runner.visited == ["start", "menu1", "menu2"]
    responses = await _drain(handler)
    assert len(responses) == 1
    assert responses[0].tts_payloads and not responses[0].end_call


@pytest.mark.asyncio
async def test_digit_and_stale_timeout_for_a_replayed_same_node_yield_one_retry():
    """A node-id comparison alone cannot tell a replay of the *same* node
    apart from the arm it replaced — `_invalid_attempt()` re-enters the
    identical `menu` node id. Only a per-arm generation counter catches
    this: a digit and an already-expired timeout for the node's *first*
    arm, enqueued in the same tick, must produce exactly one retry
    increment and no Hangup — not two retries and a wrongful
    Hangup("retries_exhausted") after a single mistaken keypress.

    Uses a long real timeout_ms (unlike _menu_timeout_graph()'s 20ms) so
    the replay's own *real* timer cannot mature and confound the assertion
    within this test's short sleep — the only timeout event in play must be
    the manually-injected stale one."""
    graph = _menu_timeout_graph()
    graph["nodes"][1]["data"]["timeout_ms"] = 5000  # "menu" node
    handler, runner, tts = _handler(graph)
    await asyncio.wait_for(handler.out_responses.get(), timeout=1)  # drain the first prompt

    assert runner.node.id == "menu"
    first_arm_generation = handler._listen_generation

    # The digit is unmatched (only "1" branches; "9" does not), so it
    # replays the same "menu" node via _invalid_attempt() — a fresh Listen,
    # same node id. The timeout event was armed for the *first* Listen and
    # must be dropped once the replay's new Listen has been armed.
    await handler.on_dtmf("s1", "9")
    handler._events.put_nowait(("timeout", first_arm_generation))
    await asyncio.sleep(0.05)

    assert runner.node.id == "menu"          # still on menu — one retry, not exhausted
    responses = await _drain(handler)
    assert len(responses) == 1               # only the replay's prompt, no Hangup
    assert responses[0].tts_payloads and not responses[0].end_call


# ── Test 14b: sensitive collect redaction, with a live negative control ────

@pytest.mark.asyncio
async def test_sensitive_collect_excluded_from_delegate_and_logs(caplog):
    captured: dict = {}
    handler, runner, tts = _handler(
        _collect_to_agent_graph(sensitive=True), handoff=_delegate_factory(captured),
    )
    await asyncio.sleep(0)
    with caplog.at_level("DEBUG"):
        for d in "9876":
            await handler.on_dtmf("s1", d)
        await asyncio.sleep(0.05)

    assert "pin" not in captured
    assert "pin" not in handler._delegate._workflow.variables
    assert "pin" not in handler._delegate._workflow.extracted_variables()
    assert not any("9876" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_non_sensitive_collect_negative_control_reaches_delegate():
    captured: dict = {}
    handler, runner, tts = _handler(
        _collect_to_agent_graph(sensitive=False), handoff=_delegate_factory(captured),
    )
    await asyncio.sleep(0)
    for d in "9876":
        await handler.on_dtmf("s1", d)
    await asyncio.sleep(0.05)

    assert captured.get("pin") == "9876"
    assert handler._delegate._workflow.variables.get("pin") == "9876"


# ── Test 15: on_session_end leaves no pending tasks ────────────────────────

@pytest.mark.asyncio
async def test_on_session_end_leaves_no_pending_tasks():
    pre_call_tasks = asyncio.all_tasks()
    handler, runner, tts = _handler(_menu_timeout_graph())
    await asyncio.sleep(0)  # armed the menu's Listen timer

    await handler.on_session_end("s1", "caller_hangup")
    await asyncio.sleep(0)

    assert asyncio.all_tasks() == pre_call_tasks


# ── Grep tripwire (T15 item v) ──────────────────────────────────────────────

def test_no_digit_shaped_format_string_in_callflow_package():
    import pathlib
    pkg = pathlib.Path(__file__).resolve().parent.parent / "callflow"
    for path in pkg.glob("*.py"):
        assert not re.search(r"digit=%s", path.read_text()), path
