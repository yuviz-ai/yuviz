"""
Dry-run tests — the whole point of keeping WorkflowRunner free of voice
concerns (docs/workflow.md §7.2). A scripted walk through a graph, in
milliseconds, with no pipeline, no audio and no providers anywhere near it.

If a change to WorkflowRunner makes these tests need a pipeline, that
change took something away.
"""

from __future__ import annotations

import asyncio

from libs.config_sdk.workflow import parse_graph

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.workflow import WorkflowRunner

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "g1", "type": "global", "data": {
            "name": "always applies", "prompt": "You are Ada, a receptionist.",
        }},
        {"id": "n1", "type": "start", "data": {
            "name": "greeting",
            "prompt": "Greet the caller and find out what they want.",
            "greeting": "Hi, thanks for calling {{ business_name | the clinic }}.",
        }},
        {"id": "n2", "type": "agent", "data": {
            "name": "booking",
            "prompt": "Book an appointment for {{ caller_number }}.",
            "tools": ["book_appointment"],
            "knowledge_base_ids": [],
            "extraction": {"enabled": True, "prompt": "Only what they said.",
                           "variables": [{"name": "reason", "type": "string",
                                          "prompt": "Why they want the appointment."}]},
        }},
        {"id": "n3", "type": "agent", "data": {
            "name": "qanda", "prompt": "Answer their question.",
            "knowledge_base_ids": ["kb1"],
        }},
        {"id": "n4", "type": "transfer", "data": {
            "name": "to_human", "prompt": "Say you're connecting them.",
            "transfer_destination": "+15559999",
        }},
        {"id": "n5", "type": "end", "data": {
            "name": "goodbye", "prompt": "Close warmly.", "disposition": "qualified",
        }},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "data": {
            "label": "caller wants to book",
            "condition": "The caller has asked to make an appointment.",
            "transition_speech": "Of course, let me pull up the calendar.",
        }},
        {"id": "e2", "source": "n1", "target": "n3", "data": {
            "label": "just a question", "condition": "The caller asked a question."}},
        {"id": "e3", "source": "n1", "target": "n4", "data": {
            "label": "wants a human", "condition": "The caller asked for a person."}},
        {"id": "e4", "source": "n2", "target": "n5", "data": {
            "label": "booked", "condition": "The appointment is booked."}},
        {"id": "e5", "source": "n3", "target": "n5", "data": {
            "label": "answered", "condition": "Their question is answered."}},
    ],
}


def _runner(**kwargs) -> WorkflowRunner:
    # No global_prompt argument: the always-on instruction is the graph's
    # own global node (g1 above), not something handed in beside it.
    kwargs.setdefault("base_suffix", "Today is 2026-08-28.")
    kwargs.setdefault("variables", {"caller_number": "+15551234"})
    return WorkflowRunner(parse_graph(GRAPH), **kwargs)


def test_qualified_path():
    runner = _runner()
    assert runner.node.name == "greeting"

    tools = runner.local_tools()
    assert set(tools) == {"goto_caller_wants_to_book", "goto_just_a_question", "goto_wants_a_human"}
    asyncio.run(tools["goto_caller_wants_to_book"][1]({}))

    assert runner.node.name == "booking"
    assert runner.allowed_tool_names() == ["book_appointment"]
    assert runner.pending_speech == "Of course, let me pull up the calendar."

    asyncio.run(runner.local_tools()["goto_booked"][1]({}))
    assert runner.node.name == "goodbye"
    assert runner.pending_end is True
    assert runner.disposition == "qualified"
    assert runner.visited == ["greeting", "booking", "goodbye"]


def test_the_booking_tool_does_not_exist_until_the_booking_node_is_active():
    # Not "the model is told not to use it" — it is genuinely not in the
    # tool list sent to the provider on turn one.
    runner = _runner()
    assert runner.allowed_tool_names() == []


def test_prompt_is_global_then_node_then_suffix_and_renders_variables():
    runner = _runner()
    asyncio.run(runner.local_tools()["goto_caller_wants_to_book"][1]({}))
    assert runner.system_prompt() == (
        "You are Ada, a receptionist.\n\n"
        "Book an appointment for +15551234.\n\n"
        "Today is 2026-08-28."
    )


def test_greeting_comes_from_the_start_node_with_a_fallback():
    assert _runner().greeting() == "Hi, thanks for calling the clinic."
    runner = _runner(variables={"business_name": "Oak Dental"})
    assert runner.greeting() == "Hi, thanks for calling Oak Dental."


def test_extracted_variables_reach_later_nodes_prompts():
    runner = _runner()
    runner.update_variables({"caller_number": "+15550000"})
    asyncio.run(runner.local_tools()["goto_caller_wants_to_book"][1]({}))
    assert "+15550000" in runner.system_prompt()


def test_extracted_variables_projection_excludes_call_context():
    runner = _runner()
    runner.update_variables({"reason": "checkup", "caller_number": "+15550000"})
    assert runner.extracted_variables() == {"reason": "checkup"}
    assert "caller_number" in runner.variables


def test_transition_swaps_the_system_prompt_inside_the_same_turn():
    # The trap in §5.3: run_turn mutates history in place, so a transition
    # that only took effect between turns would leave the rest of this
    # turn generating under the previous node's prompt.
    runner = _runner()
    history = [
        ChatMessage(role="system", content=runner.system_prompt()),
        ChatMessage(role="user", content="I'd like to book something"),
    ]
    asyncio.run(runner.local_tools(history)["goto_caller_wants_to_book"][1]({}))
    assert "Book an appointment" in history[0].content
    assert history[1].role == "user"


def test_reaching_a_transfer_node_flags_a_transfer_not_an_end():
    runner = _runner()
    asyncio.run(runner.local_tools()["goto_wants_a_human"][1]({}))
    assert runner.pending_end is False
    assert runner.pending_transfer is not None
    assert runner.pending_transfer.transfer_destination == "+15559999"


def test_knowledge_is_per_stage():
    runner = _runner()
    assert runner.knowledge_enabled() is False       # start node has no KB
    asyncio.run(runner.local_tools()["goto_just_a_question"][1]({}))
    assert runner.knowledge_enabled() is True        # q&a node does


def test_all_empty_knowledge_keeps_agent_level_rag():
    # Starter backfill omitted knowledge_base_ids — must not silently disable RAG.
    bare = {
        "version": 1,
        "nodes": [
            {"id": "g1", "type": "global", "data": {"name": "g", "prompt": "p"}},
            {"id": "n1", "type": "start", "data": {
                "name": "greeting", "prompt": "hi", "greeting": "Hi",
            }},
            {"id": "n2", "type": "end", "data": {
                "name": "goodbye", "prompt": "bye", "disposition": "completed",
            }},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2",
             "data": {"label": "done", "condition": "Finished."}},
        ],
    }
    runner = WorkflowRunner(parse_graph(bare))
    assert runner.knowledge_enabled() is True
    assert runner.allowed_tool_names() is None  # policies win on starter shape


def test_empty_global_falls_back_to_default_system_prompt():
    bare = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "start", "data": {
                "name": "greeting", "prompt": "Ask what they need.", "greeting": "Hi",
            }},
            {"id": "n2", "type": "end", "data": {
                "name": "goodbye", "prompt": "Bye", "disposition": "completed",
            }},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2",
             "data": {"label": "done", "condition": "Finished."}},
        ],
    }
    runner = WorkflowRunner(parse_graph(bare), default_global="You are the clinic receptionist.")
    assert runner.system_prompt().startswith("You are the clinic receptionist.")
    assert "Ask what they need." in runner.system_prompt()


def test_abandon_transfer_reverts_to_source_node():
    runner = _runner()
    asyncio.run(runner.local_tools()["goto_wants_a_human"][1]({}))
    assert runner.node.name == "to_human"
    assert runner.pending_transfer is not None
    runner.abandon_transfer()
    assert runner.node.name == "greeting"
    assert runner.pending_transfer is None
    assert runner.visited == ["greeting"]


def test_extraction_fires_before_leaving_the_node_not_after():
    seen: list[tuple[str, int]] = []

    class _Extractor:
        def extract(self, node, history):
            seen.append((node.name, len(history)))

    runner = _runner(extractor=_Extractor())
    asyncio.run(runner.local_tools()["goto_caller_wants_to_book"][1]({}))
    assert seen == []                       # the start node declares none
    asyncio.run(runner.local_tools()["goto_booked"][1]({}))
    # Extracted from the booking node, while booking was still the active
    # one — after the swap this segment is just historical context.
    assert seen == [("booking", 0)]


def test_no_dead_ends_and_the_happy_path_reaches_an_end_node():
    # The check every published graph should carry (§7.2).
    graph = parse_graph(GRAPH)
    for node in graph.nodes.values():
        # A global node is wired to nothing on purpose — it applies to every
        # step rather than being one.
        if node.is_unwired:
            continue
        assert node.is_terminal or node.out_edges, f"{node.name} is a dead end"
    assert any(graph.nodes[n].type == "end" for n in graph.reachable())


def test_a_graph_with_no_global_node_just_has_no_global_prefix():
    bare = {**GRAPH, "nodes": [n for n in GRAPH["nodes"] if n["type"] != "global"]}
    runner = WorkflowRunner(parse_graph(bare), base_suffix="Today is 2026-08-28.")
    assert runner.system_prompt() == (
        "Greet the caller and find out what they want.\n\nToday is 2026-08-28."
    )


def test_graph_for_fallback_seeds_greeting_and_system_prompt():
    from datetime import datetime, timezone

    from libs.config_sdk import (
        Agent, ConversationInfo, MediaInfo, Policies, ProviderConfig, ProviderConfigs,
        RuntimeConfig, Tenant,
    )
    from services.conversation.workflow.runner import _GRAPH_CACHE, graph_for

    _GRAPH_CACHE.clear()
    now = datetime.now(timezone.utc)
    placeholder = ProviderConfig(
        id="p1", role="stt", engine="fake", model=None, voice=None, language=None, api_key_ref=None,
    )
    rc = RuntimeConfig(
        tenant=Tenant(
            id="t1", slug="t", name="T", region="us",
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            no_speech_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
            transfer_timeout_ms=None,
            default_stt_config_id=None, default_llm_config_id=None, default_tts_config_id=None,
            config_version=1, updated_at=now,
        ),
        agent=Agent(
            id="a1", slug="agent", tenant_id="t1", name="Agent",
            greeting="Hi from column.", system_prompt="Be the column prompt.",
            goodbye_grace_ms=0, stt_config_id=None, llm_config_id=None, tts_config_id=None,
            status="active", config_version=1, updated_at=now,
        ),
        providers=ProviderConfigs(stt=placeholder, llm=placeholder, tts=placeholder),
        conversation=ConversationInfo(
            greeting="Hi from column.", system_prompt="Be the column prompt.", workflow=None,
        ),
        media=MediaInfo(voice=None, language=None),
        policies=Policies(
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            silence_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None, goodbye_grace_ms=0,
        ),
        tools=[], version=1, resolved_at=now,
    )
    graph = graph_for(rc)
    assert "Hi from column." in (graph.start.greeting or "")
    assert "Be the column prompt." in (graph.global_prompt or "")


def test_graph_for_fallback_preserves_tools_from_broken_published_json():
    from datetime import datetime, timezone

    from libs.config_sdk import (
        Agent, ConversationInfo, MediaInfo, Policies, ProviderConfig, ProviderConfigs,
        RuntimeConfig, Tenant,
    )
    from services.conversation.workflow.runner import _GRAPH_CACHE, graph_for

    _GRAPH_CACHE.clear()
    now = datetime.now(timezone.utc)
    placeholder = ProviderConfig(
        id="p1", role="stt", engine="fake", model=None, voice=None, language=None, api_key_ref=None,
    )
    # Missing end node → WorkflowInvalid → starter fallback must keep tools.
    broken = {
        "version": 1,
        "nodes": [
            {"id": "g1", "type": "global", "data": {"name": "g", "prompt": "Be helpful."}},
            {"id": "n1", "type": "start", "data": {
                "name": "greeting", "prompt": "hi", "greeting": "Hi",
                "tools": ["book_appointment", "send_sms"],
                "knowledge_base_ids": ["kb-1"],
            }},
        ],
        "edges": [],
    }
    rc = RuntimeConfig(
        tenant=Tenant(
            id="t1", slug="t", name="T", region="us",
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            no_speech_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None,
            transfer_timeout_ms=None,
            default_stt_config_id=None, default_llm_config_id=None, default_tts_config_id=None,
            config_version=1, updated_at=now,
        ),
        agent=Agent(
            id="a1", slug="agent", tenant_id="t1", name="Agent",
            greeting="Hi", system_prompt="Be helpful.",
            goodbye_grace_ms=0, stt_config_id=None, llm_config_id=None, tts_config_id=None,
            status="active", config_version=1, updated_at=now,
        ),
        providers=ProviderConfigs(stt=placeholder, llm=placeholder, tts=placeholder),
        conversation=ConversationInfo(
            greeting="Hi", system_prompt="Be helpful.", workflow=broken,
        ),
        media=MediaInfo(voice=None, language=None),
        policies=Policies(
            vad_engine=None, vad_onset_ms=None, vad_hold_ms=None, vad_speech_threshold=None,
            silence_timeout_ms=None, stt_timeout_ms=None, llm_timeout_ms=None, goodbye_grace_ms=0,
        ),
        tools=[], version=1, resolved_at=now,
    )
    graph = graph_for(rc)
    assert graph.start.tools == ["book_appointment", "send_sms"]
    assert graph.start.knowledge_base_ids == ["kb-1"]

