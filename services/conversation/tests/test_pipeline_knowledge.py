"""
Knowledge retrieval integration in PipelineConversationHandler.

REWRITTEN 2026-09-18. Retrieval used to be unconditional: exactly one
retrieve() call per turn, folded into that turn's user message. It is now
the `search_knowledge` local tool, offered to the model alongside
execute_api and called only when the model decides the question needs the
business's own documents (see registry.py's two-tool docstring and
pipeline.py's _local_tools).

So the contract these tests pin down changed shape:
  - a turn where the model does not ask for documents costs ZERO retrievals
  - the caller's message is never rewritten with retrieved context
  - the tool is offered only when this node actually has knowledge enabled
  - a retrieval failure is reported as a failure, never as "nothing found"
"""

from __future__ import annotations

from libs.knowledge_sdk import MockKnowledgeProvider

from services.conversation.tools.types import ToolStatus

from .test_pipeline import _make_handler, _make_llm, _make_stt, _make_tts, _silence


def _capturing_llm(tokens: list[str]):
    calls: list[list] = []

    async def _gen(messages):
        calls.append(list(messages))
        for t in tokens:
            yield t

    llm = _make_llm(tokens)
    llm.generate = _gen
    return llm, calls


async def test_no_knowledge_provider_leaves_llm_messages_unchanged():
    stt = _make_stt("What is your refund policy?")
    llm, calls = _capturing_llm(["We", " have", " a", " policy."])
    tts = _make_tts()
    handler = _make_handler(stt, llm, tts, knowledge=None)

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert len(calls) == 1
    assert [m.role for m in calls[0]] == ["system", "user"]


async def test_agent_with_no_eligible_kb_leaves_llm_messages_unchanged():
    stt = _make_stt("What is your refund policy?")
    llm, calls = _capturing_llm(["We", " have", " a", " policy."])
    tts = _make_tts()
    knowledge = MockKnowledgeProvider()  # no chunks added for ("test", "test-agent")
    handler = _make_handler(stt, llm, tts, knowledge=knowledge, node_knowledge=["kb1"])

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert [m.role for m in calls[0]] == ["system", "user"]


async def test_no_retrieval_happens_unless_the_model_asks_for_it():
    """The core behaviour change: an eligible KB is no longer enough to
    trigger a lookup. This handler has no tool orchestrator, so the model
    never calls search_knowledge — and retrieval must not happen anyway."""
    stt = _make_stt("What is your refund policy?")
    llm, calls = _capturing_llm(["We", " have", " a", " policy."])
    tts = _make_tts()

    class CountingMockKnowledgeProvider(MockKnowledgeProvider):
        def __init__(self):
            super().__init__()
            self.retrieve_calls = 0

        async def retrieve(self, tenant_slug, agent_slug, query, policy=None):
            self.retrieve_calls += 1
            return await super().retrieve(tenant_slug, agent_slug, query, policy)

    knowledge = CountingMockKnowledgeProvider()
    knowledge.add_chunk(
        "test", "test-agent", "Refunds are processed within 30 days.",
        score=0.9, document_title="Refund Policy",
    )
    handler = _make_handler(stt, llm, tts, knowledge=knowledge, node_knowledge=["kb1"])

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert knowledge.retrieve_calls == 0
    # The caller's message reaches the LLM verbatim — no context prefix,
    # and still exactly one system message.
    assert [m.role for m in calls[0]] == ["system", "user"]
    assert calls[0][1].content == "What is your refund policy?"
    assert "Refunds are processed within 30 days." not in calls[0][1].content

    history = handler._get_history("s1")
    assert [m.role for m in history] == ["system", "user", "assistant"]
    assert history[1].content == "What is your refund policy?"


async def test_search_knowledge_is_offered_and_returns_passages_when_called():
    """The other half: the tool IS wired up, and invoking it performs the
    retrieval and hands back passages plus citations."""
    stt = _make_stt("What is your refund policy?")
    llm, _calls = _capturing_llm(["ok"])
    tts = _make_tts()
    knowledge = MockKnowledgeProvider()
    knowledge.add_chunk(
        "test", "test-agent", "Refunds are processed within 30 days.",
        score=0.9, document_title="Refund Policy",
    )
    handler = _make_handler(stt, llm, tts, knowledge=knowledge, node_knowledge=["kb1"])

    tools = handler._local_tools([], None, "s1")
    assert "search_knowledge" in tools
    definition, invoke = tools["search_knowledge"]
    assert definition.parameters_schema["required"] == ["query"]

    result = await invoke({"query": "refund policy"})
    assert result.status is ToolStatus.SUCCESS
    assert result.payload["found"] is True
    assert "Refunds are processed within 30 days." in result.payload["passages"]
    assert "Refund Policy" in result.payload.get("sources", [])


async def test_search_knowledge_is_not_offered_without_a_knowledge_provider():
    """No provider configured at all — the tool must not be advertised,
    or the model would call something that can never answer."""
    stt = _make_stt("hello")
    llm, _calls = _capturing_llm(["hi"])
    tts = _make_tts()
    handler = _make_handler(stt, llm, tts, knowledge=None, node_knowledge=["kb1"])

    assert "search_knowledge" not in handler._local_tools([], None, "s1")


async def test_search_knowledge_reports_an_empty_result_as_not_found():
    stt = _make_stt("hello")
    llm, _calls = _capturing_llm(["hi"])
    tts = _make_tts()
    # No chunks added — the KB is eligible but has nothing to say.
    handler = _make_handler(
        stt, llm, tts, knowledge=MockKnowledgeProvider(), node_knowledge=["kb1"],
    )

    _defn, invoke = handler._local_tools([], None, "s1")["search_knowledge"]
    result = await invoke({"query": "refund policy"})

    assert result.status is ToolStatus.SUCCESS
    assert result.payload == {"found": False, "passages": []}


async def test_search_knowledge_reports_a_broken_lookup_as_a_failure_not_as_empty():
    """The distinction that protects the caller: "we have no policy on
    that" and "the lookup broke" must never be the same answer."""
    stt = _make_stt("hello")
    llm, _calls = _capturing_llm(["hi"])
    tts = _make_tts()

    class ExplodingKnowledgeProvider:
        async def retrieve(self, tenant_slug, agent_slug, query, policy=None):
            raise RuntimeError("boom")

        async def close(self):
            pass

    handler = _make_handler(
        stt, llm, tts, knowledge=ExplodingKnowledgeProvider(), node_knowledge=["kb1"],
    )

    _defn, invoke = handler._local_tools([], None, "s1")["search_knowledge"]
    result = await invoke({"query": "refund policy"})

    assert result.status is ToolStatus.FAILED
    assert result.error == "knowledge_unavailable"


async def test_knowledge_retrieval_exception_degrades_to_no_context_not_a_failed_turn():
    stt = _make_stt("Anything else?")
    llm, calls = _capturing_llm(["Sure."])
    tts = _make_tts()

    class ExplodingKnowledgeProvider:
        async def retrieve(self, tenant_slug, agent_slug, query, policy=None):
            raise RuntimeError("boom")

        async def close(self):
            pass

    handler = _make_handler(stt, llm, tts, knowledge=ExplodingKnowledgeProvider(), node_knowledge=["kb1"])

    responses = []
    async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        responses.append(r)

    assert responses  # the turn still completes normally
    assert [m.role for m in calls[0]] == ["system", "user"]
