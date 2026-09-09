"""
One end-to-end pass through the real pipeline: handler + orchestrator,
scripted tool-aware LLM. Covers mid-turn prompt swap, tool scoping, end node,
transition speech, and workflow transfer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from services.conversation.tools.executor_registry import ExecutorRegistry
from services.conversation.tools.llm_adapter import LLMAdapter, TokenEvent, ToolCallEvent
from services.conversation.tools.orchestrator import ToolCallOrchestrator

from .test_pipeline import _make_handler, _make_stt, _make_tts, _silence

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "g1", "type": "global", "data": {
            "name": "always applies", "prompt": "You are Ada."}},
        {"id": "n1", "type": "start", "data": {
            "name": "greeting", "prompt": "Ask what they need.",
            "greeting": "Thanks for calling."}},
        {"id": "n2", "type": "agent", "data": {
            "name": "booking", "prompt": "Take their preferred time.",
            "tools": ["book_appointment"]}},
        {"id": "n3", "type": "end", "data": {
            "name": "goodbye", "prompt": "Say goodbye.", "disposition": "qualified"}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "data": {
            "label": "wants to book", "condition": "The caller asked to book.",
            "transition_speech": "Let me pull up the calendar."}},
        {"id": "e2", "source": "n2", "target": "n3", "data": {
            "label": "booked", "condition": "The appointment is booked."}},
    ],
}

TRANSFER_GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "g1", "type": "global", "data": {
            "name": "always applies", "prompt": "You are Ada."}},
        {"id": "n1", "type": "start", "data": {
            "name": "greeting", "prompt": "Ask what they need.",
            "greeting": "Thanks for calling."}},
        {"id": "n2", "type": "transfer", "data": {
            "name": "to_human", "prompt": "Say you're connecting them.",
            "transfer_destination": "+15559999"}},
        {"id": "n3", "type": "end", "data": {
            "name": "goodbye", "prompt": "Close.", "disposition": "completed"}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "data": {
            "label": "wants a human", "condition": "The caller asked for a person."}},
        {"id": "e2", "source": "n1", "target": "n3", "data": {
            "label": "all done", "condition": "The caller is finished."}},
    ],
}


class _ScriptedToolLLM:
    def __init__(self, generations):
        self._generations = list(generations)
        self.seen_prompts: list[str] = []
        self.seen_tool_names: list[list[str]] = []

    async def generate(self, messages):
        self.seen_prompts.append(messages[0].content if messages else "")
        self.seen_tool_names.append([])
        for event in self._generations.pop(0):
            assert isinstance(event, TokenEvent)
            yield event.text

    async def generate_with_tools(self, messages, schemas, tool_choice=None):
        self.seen_prompts.append(messages[0].content if messages else "")
        self.seen_tool_names.append([s["name"] for s in schemas])
        for event in self._generations.pop(0):
            yield event


class _RecordingPolicyResolver:
    def __init__(self):
        self.seen_only: list[list[str] | None] = []

    async def enabled_tools(self, agent_id, only=None):
        self.seen_only.append(only)
        return []


def _handler(llm, resolver, *, workflow=GRAPH, **kw):
    orchestrator = ToolCallOrchestrator(
        llm_adapter=LLMAdapter(llm),
        policy_resolver=resolver,
        provider_manager=None,
        executor_registry=ExecutorRegistry(),
    )
    return _make_handler(
        _make_stt("I'd like to book an appointment"), llm, _make_tts(),
        system_prompt="You are Ada.", workflow=workflow, tool_orchestrator=orchestrator,
        **kw,
    )


async def test_a_call_walks_the_graph_and_ends_on_the_end_node():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_to_book", arguments={})],
        [TokenEvent(text="Sure."), TokenEvent(text=" What time suits you?")],
        [ToolCallEvent(tool_call_id="t2", tool_name="goto_booked", arguments={})],
        [TokenEvent(text="You're all set. Goodbye!")],
    ])
    resolver = _RecordingPolicyResolver()
    handler = _handler(llm, resolver)

    assert await handler.greeting("s1") != []

    turn1 = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
    assert handler._workflow.node.name == "booking"
    assert any(r.tts_payloads for r in turn1)
    assert not any(r.end_call for r in turn1)

    turn2 = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
    assert handler._workflow.node.name == "goodbye"
    assert any(r.end_call for r in turn2)
    assert handler._workflow.visited == ["greeting", "booking", "goodbye"]
    assert handler._workflow.disposition == "qualified"


async def test_transitions_are_offered_as_tools_and_the_prompt_swaps_mid_turn():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_to_book", arguments={})],
        [TokenEvent(text="Sure.")],
    ])
    resolver = _RecordingPolicyResolver()
    handler = _handler(llm, resolver)

    [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    assert llm.seen_tool_names[0] == ["goto_wants_to_book"]
    assert "Ask what they need." in llm.seen_prompts[0]
    assert "Take their preferred time." in llm.seen_prompts[1]
    assert llm.seen_prompts[1].startswith("You are Ada.")
    assert resolver.seen_only == [[], ["book_appointment"]]
    assert llm.seen_tool_names[1] == ["goto_booked"]


async def test_transition_speech_is_spoken_during_the_round_trip():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_to_book", arguments={})],
        [TokenEvent(text="Sure.")],
    ])
    handler = _handler(llm, _RecordingPolicyResolver())
    tts = handler._tts

    [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    spoken = [call.args[0] for call in tts.synthesize.await_args_list]
    assert "Let me pull up the calendar." in spoken
    assert spoken.index("Let me pull up the calendar.") < spoken.index("Sure.")
    assert handler._workflow.pending_speech is None
    # Authored transition speech must land in history for the LLM / transcript.
    history = handler._get_history("s1")
    assert any(
        m.role == "assistant" and "Let me pull up the calendar." in (m.content or "")
        for m in history
    )


async def test_an_end_call_marker_survives_a_transition_in_the_same_turn():
    """[[END_CALL]] must still hang up if transition speech yields in the same turn."""
    llm = _ScriptedToolLLM([
        [
            TokenEvent(text="All set. [[END_CALL]]"),
            ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_to_book", arguments={}),
        ],
        [],
    ])
    handler = _handler(llm, _RecordingPolicyResolver())

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    assert any(r.end_call for r in responses), (
        "the hang-up was lost — transition speech reset end_call"
    )


async def test_workflow_transfer_node_surfaces_a_transfer_request():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_a_human", arguments={})],
        [TokenEvent(text="Connecting you now.")],
    ])
    handler = _handler(
        llm, _RecordingPolicyResolver(), workflow=TRANSFER_GRAPH,
        transfer_type="warm", transfer_destination="+15550001111",
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    assert handler._workflow.node.name == "to_human"
    transfers = [r.transfer_request for r in responses if r.transfer_request]
    assert len(transfers) == 1
    assert transfers[0].destination == "+15559999"
    assert transfers[0].trigger == "workflow_policy"
    assert not any(r.end_call for r in responses)


async def test_workflow_transfer_rejected_when_agent_transfer_disabled():
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_a_human", arguments={})],
        [TokenEvent(text="One moment.")],
    ])
    handler = _handler(
        llm, _RecordingPolicyResolver(), workflow=TRANSFER_GRAPH,
        transfer_type="none",
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]

    # Must not park on the terminal transfer node with no edges out.
    assert handler._workflow.node.name == "greeting"
    assert handler._workflow.pending_transfer is None
    assert not any(r.transfer_request for r in responses)


async def test_session_end_persists_workflow_outcome_even_when_extraction_times_out():
    """Hangup must still write path/disposition if the final LLM extract stalls."""
    import services.conversation.pipeline as pipeline_mod

    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_booked", arguments={})],
        [TokenEvent(text="Bye!")],
    ])
    # Start already on booking so we can end in one transition.
    graph = {
        "version": 1,
        "nodes": [
            {"id": "g1", "type": "global", "data": {"name": "g", "prompt": "Ada."}},
            {"id": "n2", "type": "start", "data": {
                "name": "booking", "prompt": "Book.", "greeting": "Hi.",
                "extraction": {"enabled": True, "variables": [
                    {"name": "reason", "type": "string", "prompt": "why"}]},
            }},
            {"id": "n3", "type": "end", "data": {
                "name": "goodbye", "prompt": "Bye.", "disposition": "qualified"}},
        ],
        "edges": [
            {"id": "e2", "source": "n2", "target": "n3", "data": {
                "label": "booked", "condition": "done"}},
        ],
    }
    handler = _handler(llm, _RecordingPolicyResolver(), workflow=graph)
    transcripts = MagicMock()
    handler._transcripts = transcripts

    async def _stall(_sid):
        await asyncio.Event().wait()

    handler._finalize_extraction = _stall  # type: ignore[method-assign]
    original_timeout = pipeline_mod._FINISH_WORKFLOW_TIMEOUT_S
    pipeline_mod._FINISH_WORKFLOW_TIMEOUT_S = 0.05
    try:
        [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
        await handler.on_session_end("s1", "hangup")
    finally:
        pipeline_mod._FINISH_WORKFLOW_TIMEOUT_S = original_timeout

    transcripts.record_workflow_outcome.assert_called_once()
    kwargs = transcripts.record_workflow_outcome.call_args.kwargs
    assert kwargs["disposition"] == "qualified"
    assert kwargs["nodes_visited"] == ["booking", "goodbye"]
    assert "caller_number" not in (kwargs["extracted_variables"] or {})


async def test_transfer_flush_timeout_still_routes_and_leaves_straggler():
    """A stuck extract must not block transfer; wait must not cancel it."""
    import services.conversation.pipeline as pipeline_mod

    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_a_human", arguments={})],
        [TokenEvent(text="Connecting you now.")],
    ])
    handler = _handler(
        llm, _RecordingPolicyResolver(), workflow=TRANSFER_GRAPH,
        transfer_type="warm", transfer_destination="+15550001111",
    )

    async def _hang():
        await asyncio.Event().wait()

    straggler = asyncio.ensure_future(_hang())
    # Inject via pending_tasks so on_speech_ended's interrupt does not cancel it —
    # we are testing the transfer wait path only.
    handler._extractor.pending_tasks = lambda: {straggler}  # type: ignore[method-assign]
    original = pipeline_mod._TRANSFER_FLUSH_TIMEOUT_S
    pipeline_mod._TRANSFER_FLUSH_TIMEOUT_S = 0.05
    try:
        responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
        assert not straggler.done(), "wait timeout must not cancel the extract"
    finally:
        pipeline_mod._TRANSFER_FLUSH_TIMEOUT_S = original
        straggler.cancel()
        try:
            await straggler
        except asyncio.CancelledError:
            pass

    transfers = [r.transfer_request for r in responses if r.transfer_request]
    assert len(transfers) == 1
    assert transfers[0].destination == "+15559999"


async def test_transfer_rejects_injected_destination_from_extracted_variable():
    """Rendered destinations must pass transfer_destination_problem before dial."""
    graph = {
        "version": 1,
        "nodes": [
            {"id": "g1", "type": "global", "data": {
                "name": "always applies", "prompt": "You are Ada."}},
            {"id": "n1", "type": "start", "data": {
                "name": "greeting", "prompt": "Ask.", "greeting": "Hi."}},
            {"id": "n2", "type": "transfer", "data": {
                "name": "to_human", "prompt": "Connect.",
                "transfer_destination": "{{ callback_number }}"}},
            {"id": "n3", "type": "end", "data": {
                "name": "goodbye", "prompt": "Close.", "disposition": "completed"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "data": {
                "label": "wants a human", "condition": "caller wants a person"}},
            {"id": "e2", "source": "n1", "target": "n3", "data": {
                "label": "all done", "condition": "done"}},
        ],
    }
    llm = _ScriptedToolLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_a_human", arguments={})],
        [TokenEvent(text="One moment.")],
    ])
    handler = _handler(
        llm, _RecordingPolicyResolver(), workflow=graph,
        transfer_type="warm", transfer_destination="+15550001111",
    )
    handler._workflow.update_variables({
        "callback_number": "+15551212\nbgapi reloadxml",
    })
    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
    assert not any(r.transfer_request for r in responses)
    assert handler._workflow.node.name == "greeting"


async def test_next_turn_interrupts_background_extract_before_live_generate():
    """Nothing background may hold the shared LLM while generate_with_tools runs."""
    extract_started = asyncio.Event()
    extract_cancelled = asyncio.Event()
    live_saw_clear = asyncio.Event()
    handler_box: list = []

    class _TrackingLLM(_ScriptedToolLLM):
        async def generate(self, messages):
            extract_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                extract_cancelled.set()
                raise
            yield "{}"

        async def generate_with_tools(self, messages, schemas, tool_choice=None):
            h = handler_box[0]
            assert not h._extractor.pending_tasks()
            assert not h._summarizer.has_background_work()
            live_saw_clear.set()
            async for event in super().generate_with_tools(messages, schemas, tool_choice):
                yield event

    graph = {
        "version": 1,
        "nodes": [
            {"id": "g1", "type": "global", "data": {
                "name": "always applies", "prompt": "You are Ada."}},
            {"id": "n1", "type": "start", "data": {
                "name": "greeting", "prompt": "Ask.", "greeting": "Hi.",
                "extraction": {"enabled": True, "variables": [
                    {"name": "reason", "type": "string", "prompt": "why"}]},
            }},
            {"id": "n2", "type": "agent", "data": {
                "name": "booking", "prompt": "Book."}},
            {"id": "n3", "type": "end", "data": {
                "name": "goodbye", "prompt": "Bye.", "disposition": "qualified"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "data": {
                "label": "wants to book", "condition": "book"}},
            {"id": "e2", "source": "n2", "target": "n3", "data": {
                "label": "booked", "condition": "done"}},
        ],
    }
    llm = _TrackingLLM([
        [ToolCallEvent(tool_call_id="t1", tool_name="goto_wants_to_book", arguments={})],
        [TokenEvent(text="Sure.")],
        [TokenEvent(text="What time?")],
    ])
    handler = _handler(llm, _RecordingPolicyResolver(), workflow=graph)
    handler_box.append(handler)

    [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
    await asyncio.wait_for(extract_started.wait(), timeout=1.0)
    assert handler._extractor.pending_tasks()

    [r async for r in handler.on_speech_ended("s1", _silence(), 1200, -20.0)]
    await asyncio.wait_for(live_saw_clear.wait(), timeout=1.0)
    await asyncio.wait_for(extract_cancelled.wait(), timeout=1.0)
