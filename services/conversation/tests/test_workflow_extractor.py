"""
The two background LLM passes. Both are allowed to do nothing; neither is
ever allowed to break the call, so most of what's asserted here is what
happens when things go wrong.
"""

from __future__ import annotations

import asyncio

from libs.config_sdk.workflow import Extraction, ExtractionVariable, Node

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.workflow import ContextSummarizer, VariableExtractor
from services.conversation.workflow.extractor import (
    _EXTRACT_SYSTEM,
    _MAX_EXTRACTED_STR_LEN,
    _SUMMARY_KEEP_LAST,
    _coerce,
    _strip_transition_noise,
    summary_threshold_for,
)


class _FakeLLM:
    def __init__(self, reply: str = "{}") -> None:
        self._reply = reply
        self.calls: list[list[ChatMessage]] = []

    async def generate(self, messages):
        self.calls.append(list(messages))
        yield self._reply


def _node(**extraction) -> Node:
    """An agent node that declares one string variable unless told otherwise."""
    spec = Extraction(
        enabled=extraction.get("enabled", True),
        prompt="",
        variables=tuple(
            ExtractionVariable(name=n, type=t, prompt="")
            for n, t in extraction.get("variables", [("policy_number", "string")])
        ),
    )
    return Node(id="n1", type="agent", name="verify", prompt="", extraction=spec)


def test_extraction_merges_typed_values():
    llm = _FakeLLM('```json\n{"policy_number": "AB-1", "wants_callback": "yes", "unsaid": null}\n```')
    got: dict = {}
    extractor = VariableExtractor(llm, got.update)

    asyncio.run(extractor._extract(
        _node(variables=[("policy_number", "string"), ("wants_callback", "boolean"), ("unsaid", "string")]),
        [ChatMessage(role="user", content="my policy is AB-1")],
    ))

    # null means "the caller didn't say it" — never merged, so it can't
    # blank out something an earlier node captured.
    assert got == {"policy_number": "AB-1", "wants_callback": True}


def test_boolean_unknown_token_is_dropped_not_coerced_to_false():
    llm = _FakeLLM('{"wants_callback": "not sure"}')
    got: dict = {}
    extractor = VariableExtractor(llm, got.update)
    asyncio.run(extractor._extract(
        _node(variables=[("wants_callback", "boolean")]),
        [ChatMessage(role="user", content="maybe")],
    ))
    assert got == {}


def test_coerce_rejects_control_chars_and_caps_length():
    assert _coerce("555\nbgapi reloadxml", "string") is None
    assert _coerce("ok\rcmd", "string") is None
    assert _coerce("x" * (_MAX_EXTRACTED_STR_LEN + 50), "string") == "x" * _MAX_EXTRACTED_STR_LEN
    assert _coerce("+15551212", "string") == "+15551212"


def test_extraction_sends_json_only_system_prompt():
    llm = _FakeLLM('{"policy_number": "AB-1"}')
    extractor = VariableExtractor(llm, lambda _: None)
    asyncio.run(extractor._extract(_node(), [ChatMessage(role="user", content="AB-1")]))
    assert llm.calls[0][0].role == "system"
    assert llm.calls[0][0].content == _EXTRACT_SYSTEM
    assert llm.calls[0][1].role == "user"


def test_extract_queues_until_start_deferred():
    llm = _FakeLLM('{"policy_number": "AB-1"}')
    got: dict = {}
    extractor = VariableExtractor(llm, got.update)
    extractor.extract(_node(), [ChatMessage(role="user", content="AB-1")])
    assert llm.calls == []
    assert got == {}

    async def _run():
        extractor.start_deferred()
        await extractor.flush()

    asyncio.run(_run())
    assert got == {"policy_number": "AB-1"}


def test_interrupt_for_live_turn_requeues_in_flight_extract():
    started = asyncio.Event()

    class _HangLLM:
        async def generate(self, messages):
            started.set()
            await asyncio.Event().wait()
            yield "{}"

    got: dict = {}
    extractor = VariableExtractor(_HangLLM(), got.update)
    extractor.extract(_node(), [ChatMessage(role="user", content="AB-1")])

    async def _run():
        extractor.start_deferred()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert extractor.pending_tasks()
        extractor.interrupt_for_live_turn()
        assert extractor.pending_tasks() == set()
        assert len(extractor._deferred) == 1
        # Swap in a finishing LLM so the re-queued extract can land.
        extractor._llm = _FakeLLM('{"policy_number": "AB-1"}')
        extractor.start_deferred()
        await extractor.flush()

    asyncio.run(_run())
    assert got == {"policy_number": "AB-1"}


def test_cancel_pending_drops_queued_and_in_flight_work():
    started = asyncio.Event()

    class _HangLLM:
        async def generate(self, messages):
            started.set()
            await asyncio.Event().wait()
            yield "{}"

    extractor = VariableExtractor(_HangLLM(), lambda _: None)
    extractor.extract(_node(), [])
    extractor.extract(_node(), [])  # second stays queued until start drains first loop

    async def _run():
        extractor.start_deferred()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        extractor.cancel_pending()
        await extractor.flush()

    asyncio.run(_run())
    assert extractor._deferred == []
    assert extractor._pending == set()


def test_a_node_with_no_extraction_config_never_calls_the_llm():
    llm = _FakeLLM()
    extractor = VariableExtractor(llm, lambda _: None)
    end_node = Node(id="n9", type="end", name="goodbye", prompt="")

    asyncio.run(extractor.extract_final(end_node, []))
    extractor.extract(end_node, [])

    assert llm.calls == []


def test_extraction_disabled_is_honored_at_teardown_too():
    llm = _FakeLLM()
    extractor = VariableExtractor(llm, lambda _: None)
    asyncio.run(extractor.extract_final(_node(enabled=False), []))
    assert llm.calls == []


def test_the_final_pass_runs_once_however_many_teardowns_converge():
    llm = _FakeLLM('{"policy_number": "AB-1"}')
    extractor = VariableExtractor(llm, lambda _: None)

    async def _both():
        await extractor.extract_final(_node(), [])
        await extractor.extract_final(_node(), [])

    asyncio.run(_both())
    assert len(llm.calls) == 1


def test_a_broken_llm_reply_never_raises():
    llm = _FakeLLM("I'm afraid I can't do that.")
    extractor = VariableExtractor(llm, lambda _: None)
    asyncio.run(extractor._extract(_node(), []))   # logged, not raised


def test_strip_drops_goto_traffic_but_keeps_real_tools():
    history = [
        ChatMessage(role="system", content="p"),
        ChatMessage(role="user", content="book me"),
        ChatMessage(
            role="assistant", content="",
            tool_calls=[{"id": "g1", "name": "goto_wants_to_book", "arguments": {}}],
        ),
        ChatMessage(role="tool", content='{"status": "done"}', tool_call_id="g1"),
        ChatMessage(
            role="assistant", content="",
            tool_calls=[{"id": "b1", "name": "book_appointment", "arguments": {}}],
        ),
        ChatMessage(role="tool", content='{"ok": true}', tool_call_id="b1"),
        ChatMessage(role="assistant", content="Booked for Tuesday."),
    ]
    kept = _strip_transition_noise(history)
    assert not any(
        m.tool_calls and any(str(tc.get("name") or "").startswith("goto_") for tc in m.tool_calls)
        for m in kept
    )
    assert any(m.tool_call_id == "b1" for m in kept)
    assert any(m.content == "Booked for Tuesday." for m in kept)


def _long_history(n: int) -> list[ChatMessage]:
    history = [ChatMessage(role="system", content="prompt")]
    for i in range(n):
        history.append(ChatMessage(role="user", content=f"caller {i}"))
        history.append(ChatMessage(role="assistant", content=f"agent {i}"))
    return history


def test_summary_replaces_the_older_half_and_keeps_the_system_prompt():
    history = _long_history(15)
    summarizer = ContextSummarizer(_FakeLLM("They gave their DOB and postcode."), keep_last=4)

    asyncio.run(summarizer._summarize(history))

    assert history[0].role == "system"
    assert "They gave their DOB" in history[1].content
    assert history[-1].content == "agent 14"       # recent turns kept verbatim
    assert len(history) == 6


def test_summary_never_orphans_a_tool_result():
    history = _long_history(10)
    history.append(ChatMessage(role="assistant", content="", tool_calls=[{"id": "c1", "name": "book"}]))
    history.append(ChatMessage(role="tool", content='{"status":"ok"}', tool_call_id="c1"))
    history.append(ChatMessage(role="assistant", content="Booked."))
    summarizer = ContextSummarizer(_FakeLLM("Earlier context."), keep_last=2)

    asyncio.run(summarizer._summarize(history))

    tool_indexes = [i for i, m in enumerate(history) if m.role == "tool"]
    for i in tool_indexes:
        prior = history[i - 1]
        assert prior.role == "assistant" and prior.tool_calls, "tool result lost its call"


def test_summary_apply_survives_a_concurrent_trim():
    """Trim rewrites indexes while summarize awaits — identity splice must not
    delete the retained recent turns."""
    history = _long_history(15)
    recent_tail = list(history[-4:])

    class _TrimDuringGenerate:
        async def generate(self, messages):
            history[1:] = history[-20:]
            yield "Earlier they asked about hours."

    asyncio.run(ContextSummarizer(_TrimDuringGenerate(), keep_last=4)._summarize(history))

    assert history[0].role == "system"
    assert "Earlier they asked" in history[1].content
    for msg in recent_tail:
        assert msg in history, "trim+summary must not drop the live tail"


def test_summary_bails_if_trim_already_dropped_the_older_slice():
    history = _long_history(15)

    class _DropOlder:
        async def generate(self, messages):
            history[1:] = history[-4:]
            yield "stale summary"

    before_tail = list(history[-4:])
    asyncio.run(ContextSummarizer(_DropOlder(), keep_last=4)._summarize(history))
    assert history[1:] == before_tail
    assert not any("stale summary" in (m.content or "") for m in history)


def test_a_failing_summary_keeps_the_full_context():
    class _Broken:
        async def generate(self, messages):
            raise RuntimeError("provider down")
            yield ""   # pragma: no cover

    history = _long_history(15)
    before = list(history)
    asyncio.run(ContextSummarizer(_Broken())._summarize(history))
    assert history == before


def test_the_summary_threshold_pins_formula_and_keeps_trim_slack():
    """Pins expected values; the -4 slack must stay load-bearing (lesson 12)."""
    assert summary_threshold_for(1) == 2
    assert summary_threshold_for(5) == 6
    assert summary_threshold_for(10) == 16
    assert summary_threshold_for(40) == 76
    for max_history in (10, 40):
        with_slack = summary_threshold_for(max_history)
        # Same formula without the -4 would sit at the trim floor and leave
        # no room for the summary message under max_history*2+1.
        without_slack = min(
            max(_SUMMARY_KEEP_LAST + 2, max_history * 2),
            max_history * 2,
        )
        assert with_slack == max_history * 2 - 4
        assert without_slack == max_history * 2
        assert with_slack < without_slack

