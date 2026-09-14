"""
Knowledge retrieval integration in PipelineConversationHandler.on_speech_ended():
exactly one MockKnowledgeProvider.retrieve() call per turn, folded into
that turn's own user-message content for the LLM call only (never
persisted into history) — deliberately never sent as a second system-role
message, since a mid-conversation system message is out-of-distribution
for chat-tuned models and measurably degrades adherence to the *first*
system message's instructions (notably the end-call marker). A completely
unaffected code path when knowledge=None or the agent has no eligible KB
is the backward-compatibility requirement this feature was built under.
"""

from __future__ import annotations

from libs.knowledge_sdk import MockKnowledgeProvider

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


async def test_eligible_kb_folds_context_into_user_turn_only_for_this_call():
    stt = _make_stt("What is your refund policy?")
    llm, calls = _capturing_llm(["We", " have", " a", " policy."])
    tts = _make_tts()
    knowledge = MockKnowledgeProvider()
    knowledge.add_chunk(
        "test", "test-agent", "Refunds are processed within 30 days.",
        score=0.9, document_title="Refund Policy",
    )
    handler = _make_handler(stt, llm, tts, knowledge=knowledge, node_knowledge=["kb1"])

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    # Exactly one message, role=user — never a second system-role message.
    assert [m.role for m in calls[0]] == ["system", "user"]
    assert "Refunds are processed within 30 days." in calls[0][1].content
    assert "Refund Policy" in calls[0][1].content  # citation surfaced by default
    assert "What is your refund policy?" in calls[0][1].content

    # The injected context must never leak into persistent history — only
    # the real user/assistant turn pair (with the caller's actual raw
    # text, not the context-augmented version) should be there afterward.
    history = handler._get_history("s1")
    assert [m.role for m in history] == ["system", "user", "assistant"]
    assert history[1].content == "What is your refund policy?"


async def test_second_turn_makes_exactly_one_more_retrieve_call_not_zero_not_two():
    stt = _make_stt("Anything else?")
    llm, calls = _capturing_llm(["Sure."])
    tts = _make_tts()

    class CountingMockKnowledgeProvider(MockKnowledgeProvider):
        def __init__(self):
            super().__init__()
            self.retrieve_calls = 0

        async def retrieve(self, tenant_slug, agent_slug, query, policy=None):
            self.retrieve_calls += 1
            return await super().retrieve(tenant_slug, agent_slug, query, policy)

    knowledge = CountingMockKnowledgeProvider()
    knowledge.add_chunk("test", "test-agent", "Some fact.", score=0.9)
    handler = _make_handler(stt, llm, tts, knowledge=knowledge, node_knowledge=["kb1"])

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass
    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert knowledge.retrieve_calls == 2


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
