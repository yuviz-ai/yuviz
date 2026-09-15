"""
Text-chat testing (SessionOpenRequest.text_only). The claim this mode makes
is that a workflow exercised by typing is the *same* workflow a caller
reaches — same graph walk, same per-node tools, same end-call handling —
with only STT and TTS removed. These are the assertions that claim survives.
"""

from __future__ import annotations

import pytest

from services.conversation.event_bus import EventBus
from services.conversation.fsm import CallFsmState
from services.conversation.session import ConversationSession, SessionContext
from services.conversation.tools.executor_registry import ExecutorRegistry
from services.conversation.tools.llm_adapter import LLMAdapter, TokenEvent, ToolCallEvent
from services.conversation.tools.orchestrator import ToolCallOrchestrator
from services.conversation.workflow.runner import _GRAPH_CACHE

from .test_pipeline import _make_handler, _make_llm, _make_stt, _make_tts
from .test_workflow_pipeline import GRAPH, _RecordingPolicyResolver, _ScriptedToolLLM

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    _GRAPH_CACHE.clear()
    yield
    _GRAPH_CACHE.clear()


def _chat(llm, resolver=None, **kw):
    orchestrator = None
    if resolver is not None:
        orchestrator = ToolCallOrchestrator(
            llm_adapter=LLMAdapter(llm),
            policy_resolver=resolver,
            provider_manager=None,
            executor_registry=ExecutorRegistry(),
        )
    return _make_handler(
        _make_stt("never used"), llm, _make_tts(),
        text_only=True, tool_orchestrator=orchestrator, **kw,
    )


# ── the same graph walk, without a microphone ─────────────────────────────

async def test_typing_walks_the_graph_and_ends_on_the_end_node():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_to_book", arguments={})],
        [TokenEvent(text="Sure."), TokenEvent(text=" What time suits you?")],
        [ToolCallEvent(tool_call_id="t2", tool_name="goto_booked", arguments={})],
        [TokenEvent(text="You're all set. Goodbye!")],
    ])
    handler = _chat(llm, _RecordingPolicyResolver(), system_prompt="You are Ada.", workflow=GRAPH)

    turn1 = [r async for r in handler.on_text("s1", "I'd like to book")]
    assert handler._workflow.node.name == "booking"
    assert "What time suits you?" in "".join(r.agent_text for r in turn1)
    assert not any(r.end_call for r in turn1)

    turn2 = [r async for r in handler.on_text("s1", "Tuesday at 3")]
    assert handler._workflow.node.name == "goodbye"
    assert any(r.end_call for r in turn2)
    assert handler._workflow.visited == ["greeting", "booking", "goodbye"]


async def test_the_tts_provider_is_never_touched():
    # Loading a voice model costs seconds and produces audio nobody plays;
    # worse, a broken/unconfigured TTS would fail a chat that has no reason
    # to care about it.
    tts = _make_tts()
    handler = _make_handler(_make_stt("x"), _make_llm(["Hello."]), tts, text_only=True)
    [r async for r in handler.on_text("s1", "hi")]
    tts.synthesize.assert_not_awaited()


async def test_the_greeting_comes_back_as_text():
    handler = _chat(_make_llm([]), workflow=GRAPH)
    assert await handler.greeting("s1") == []          # nothing synthesized
    assert handler.greeting_message() == "Thanks for calling."


# ── edge cases ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_blank_input_is_not_a_turn(text):
    # An empty send must not append a user message to history — a stray
    # Enter would otherwise leave the conversation with an unanswered turn.
    llm = _make_llm(["should not be reached"])
    handler = _chat(llm)
    assert [r async for r in handler.on_text("s1", text)] == []
    assert handler._get_history("s1") == []


async def test_an_llm_failure_still_answers():
    # No audio path means the usual "speak a fallback" line produces
    # nothing — the words have to reach the chat some other way, or a
    # failed turn looks like the agent ignored you.
    async def _boom(messages):
        raise RuntimeError("provider down")
        yield  # pragma: no cover — makes this an async generator

    llm = _make_llm([])
    llm.generate = _boom
    handler = _chat(llm)
    replies = [r.agent_text for r in [x async for x in handler.on_text("s1", "hi")] if r.agent_text]
    assert replies == ["Sorry, I'm having a little trouble right now. Could you say that again?"]


async def test_the_reply_is_echoed_back_as_the_user_turn():
    # The client renders what the service actually processed, not the
    # string it optimistically drew.
    handler = _chat(_make_llm(["Hi."]))
    responses = [r async for r in handler.on_text("s1", "  hello  ")]
    assert responses[0].stt_text == "hello"


# ── the session-level FSM, which is where a second message goes missing ───

class _Handler:
    """Minimal IConversationHandler: one text turn, no audio anywhere."""

    def __init__(self):
        self.seen: list[str] = []

    async def greeting(self, session_id):
        return []

    def greeting_message(self):
        return "Hello."

    async def on_text(self, session_id, text):
        from services.conversation.session import HandlerResponse
        self.seen.append(text)
        yield HandlerResponse(stt_text=text, stt_confidence=1.0)
        yield HandlerResponse(agent_text=f"you said {text}")

    async def on_audio(self, session_id, payload):
        from services.conversation.session import HandlerResponse
        return HandlerResponse()

    async def on_cancel(self, session_id):
        pass

    async def on_session_end(self, session_id, reason, final_state=None):
        pass


def _session(handler):
    ctx = SessionContext(session_id="s1", tenant_id="t", script_id="a", text_only=True)
    session = ConversationSession(ctx=ctx, bus=EventBus(), handler=handler)
    session.session_ready()
    return session


async def test_every_message_in_a_conversation_is_processed():
    # A text turn never reaches SPEAKING and no playback_finished is ever
    # coming, so without an explicit reset the FSM sits mid-turn and the
    # guard at the top of text_input() silently drops message two onward.
    handler = _Handler()
    session = _session(handler)
    for i in range(3):
        replies = [r async for r in session.text_input(f"msg{i}")]
        assert any(r.agent_text for r in replies), f"turn {i} produced nothing"
        assert session.fsm_state is CallFsmState.LISTENING
    assert handler.seen == ["msg0", "msg1", "msg2"]


async def test_a_typed_turn_does_not_claim_an_stt_or_tts_engine():
    # Regression: the turn record named whisper and kokoro on a turn where
    # neither ran, which reads in call analytics as if it had been spoken.
    from unittest.mock import MagicMock

    handler = _make_handler(_make_stt("x"), _make_llm(["Hello."]), _make_tts(), text_only=True)
    handler._transcripts = MagicMock()
    [r async for r in handler.on_text("s1", "hi")]
    latency = handler._transcripts.record_turn.call_args.kwargs["latency"]
    assert latency.stt_engine is None and latency.tts_engine is None


async def test_the_opening_line_is_delivered_in_a_chat():
    session = _session(_Handler())
    assert [r.agent_text for r in [x async for x in session.greet()]] == ["Hello."]


async def test_a_blank_turn_leaves_the_session_usable():
    # A stray Enter drives the FSM into RECOGNIZING before the handler
    # decides there is nothing to answer. If that isn't unwound, the very
    # next real message is dropped by the guard at the top of text_input.
    handler = _Handler()
    session = _session(handler)
    [_ async for _ in session.text_input("   ")]
    assert session.fsm_state is CallFsmState.LISTENING
    replies = [r async for r in session.text_input("hello")]
    assert any(r.agent_text for r in replies)


async def test_text_only_tool_fillers_do_not_enter_assistant_history():
    """Voice fillers yield ("", tts); text must not put the phrase in full_response."""
    from services.conversation.tools.llm_adapter import TokenEvent, ToolCallStartedEvent
    from .test_pipeline import _FakeToolOrchestrator

    orchestrator = _FakeToolOrchestrator([
        ToolCallStartedEvent(tool_name="book_appointment"),
        TokenEvent(text="You're booked."),
    ])
    handler = _make_handler(
        _make_stt("x"), _make_llm(["unused"]), _make_tts(),
        text_only=True, tool_orchestrator=orchestrator, has_booking_tool=True,
    )
    [r async for r in handler.on_text("s1", "book me")]
    history = handler._session("s1").history
    assistant = [m.content for m in history if m.role == "assistant"]
    assert assistant == ["You're booked."]


async def test_use_workflow_draft_runs_the_draft_graph_not_published():
    published = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "start", "position": {"x": 0, "y": 0},
             "data": {"name": "live", "prompt": "Published.", "greeting": "Live hello."}},
            {"id": "n2", "type": "end", "position": {"x": 0, "y": 190},
             "data": {"name": "bye", "prompt": "Done.", "disposition": "completed"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2",
             "data": {"label": "done", "condition": "Caller is done."}},
        ],
    }
    draft = {
        "version": 1,
        "nodes": [
            {"id": "d1", "type": "start", "position": {"x": 0, "y": 0},
             "data": {"name": "draft-start", "prompt": "Draft.", "greeting": "Draft hello."}},
            {"id": "d2", "type": "end", "position": {"x": 0, "y": 190},
             "data": {"name": "draft-end", "prompt": "Draft bye.", "disposition": "completed"}},
        ],
        "edges": [
            {"id": "de1", "source": "d1", "target": "d2",
             "data": {"label": "finish", "condition": "Caller is done."}},
        ],
    }
    handler = _make_handler(
        _make_stt("x"), _make_llm(["ok"]), _make_tts(),
        text_only=True, workflow=published, workflow_draft=draft, use_workflow_draft=True,
    )
    assert handler._workflow.node.name == "draft-start"
    assert handler.greeting_message() == "Draft hello."
    opening = handler.opening_events()
    assert any(r.node_changed and r.node_changed.node_name == "draft-start" for r in opening)
    assert not handler._draft_fell_back


async def test_invalid_draft_falls_back_and_notes_it():
    published = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "start", "position": {"x": 0, "y": 0},
             "data": {"name": "live", "prompt": "Published.", "greeting": "Live hello."}},
            {"id": "n2", "type": "end", "position": {"x": 0, "y": 190},
             "data": {"name": "bye", "prompt": "Done.", "disposition": "completed"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2",
             "data": {"label": "done", "condition": "Caller is done."}},
        ],
    }
    # Dead-end draft: agent node with no outbound edge — parse/validate fails.
    bad_draft = {
        "version": 1,
        "nodes": [
            {"id": "d1", "type": "start", "position": {"x": 0, "y": 0},
             "data": {"name": "broken", "prompt": "x", "greeting": "Nope."}},
            {"id": "d2", "type": "agent", "position": {"x": 0, "y": 190},
             "data": {"name": "stuck", "prompt": "y"}},
        ],
        "edges": [
            {"id": "e1", "source": "d1", "target": "d2",
             "data": {"label": "go", "condition": "go"}},
        ],
    }
    handler = _make_handler(
        _make_stt("x"), _make_llm(["ok"]), _make_tts(),
        text_only=True, workflow=published, workflow_draft=bad_draft, use_workflow_draft=True,
    )
    assert handler._draft_fell_back
    assert handler._workflow.node.name == "live"
    notes = [r.session_note for r in handler.opening_events() if r.session_note]
    assert notes and "doesn't parse" in notes[0]
