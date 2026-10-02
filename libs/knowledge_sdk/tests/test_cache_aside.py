"""CacheAsideKnowledgeProvider orchestration tests against fake repositories."""

from __future__ import annotations

from libs.knowledge_sdk.exceptions import RepositoryUnavailableError
from libs.knowledge_sdk.models import RetrievalPolicy
from libs.knowledge_sdk.providers.cache_aside import CacheAsideKnowledgeProvider


class FakeAvailabilityRepo:
    def __init__(self, value: bool | None = None):
        self.value = value
        self.calls = 0
        self.closed = False

    async def has_enabled_kb(self, tenant_slug, agent_slug):
        self.calls += 1
        return self.value

    async def close(self):
        self.closed = True


class FakeRetrievalRepo:
    def __init__(self, raw=None, has_kb=True, unavailable=False):
        self.raw = raw
        self.has_kb = has_kb
        self.unavailable = unavailable
        self.retrieve_calls: list[str] = []
        self.closed = False

    async def has_enabled_kb(self, tenant_slug, agent_slug):
        return self.has_kb

    async def retrieve(self, tenant_slug, agent_slug, query, policy):
        self.retrieve_calls.append(query)
        if self.unavailable:
            raise RepositoryUnavailableError("down")
        return self.raw

    async def close(self):
        self.closed = True


def _raw_context(**overrides):
    return {
        "chunks": [
            {
                "content": "The refund window is 30 days.",
                "score": 0.87,
                "source": {
                    "kb_id": "kb1", "kb_slug": "policies", "document_id": "doc1",
                    "document_title": "Refund Policy", "chunk_id": "c1", "page": 2,
                    "language": "en", "tags": {}, "version": 1,
                },
            },
        ],
        "sources": ["Refund Policy"],
        "token_count": 8,
        "retrieval_metadata": {"top_k": 5},
        **overrides,
    }


async def test_redis_false_short_circuits_before_any_http_retrieve_call():
    availability = FakeAvailabilityRepo(value=False)
    retrieval = FakeRetrievalRepo(raw=_raw_context())
    provider = CacheAsideKnowledgeProvider(availability, retrieval)

    result = await provider.retrieve("acme", "sup", "how do refunds work?")

    assert result is None
    assert retrieval.retrieve_calls == []


async def test_redis_true_calls_retrieve_and_maps_context():
    availability = FakeAvailabilityRepo(value=True)
    retrieval = FakeRetrievalRepo(raw=_raw_context())
    provider = CacheAsideKnowledgeProvider(availability, retrieval)

    result = await provider.retrieve("acme", "sup", "how do refunds work?")

    assert result is not None
    assert result.chunks[0].content == "The refund window is 30 days."
    assert result.chunks[0].source.document_title == "Refund Policy"
    assert result.sources == ["Refund Policy"]
    assert result.confidence == 0.87
    assert result.token_count == 8


async def test_redis_miss_falls_through_to_http_has_enabled_kb():
    availability = FakeAvailabilityRepo(value=None)  # cache miss
    retrieval = FakeRetrievalRepo(raw=_raw_context(), has_kb=True)
    provider = CacheAsideKnowledgeProvider(availability, retrieval)

    result = await provider.retrieve("acme", "sup", "q")

    assert result is not None
    assert availability.calls == 1


async def test_redis_miss_and_http_says_no_kb_returns_none():
    availability = FakeAvailabilityRepo(value=None)
    retrieval = FakeRetrievalRepo(raw=_raw_context(), has_kb=False)
    provider = CacheAsideKnowledgeProvider(availability, retrieval)

    assert await provider.retrieve("acme", "sup", "q") is None
    assert retrieval.retrieve_calls == []


async def test_retrieval_repo_unavailable_degrades_to_none_not_exception():
    availability = FakeAvailabilityRepo(value=True)
    retrieval = FakeRetrievalRepo(unavailable=True)
    provider = CacheAsideKnowledgeProvider(availability, retrieval)

    assert await provider.retrieve("acme", "sup", "q") is None


async def test_no_chunks_in_raw_response_yields_confidence_zero():
    availability = FakeAvailabilityRepo(value=True)
    retrieval = FakeRetrievalRepo(raw=_raw_context(chunks=[], sources=[], token_count=0))
    provider = CacheAsideKnowledgeProvider(availability, retrieval)

    result = await provider.retrieve("acme", "sup", "q")

    assert result is not None
    assert result.chunks == []
    assert result.confidence == 0.0


async def test_policy_is_passed_through_to_retrieval_repo():
    availability = FakeAvailabilityRepo(value=True)

    captured = {}

    class CapturingRetrievalRepo(FakeRetrievalRepo):
        async def retrieve(self, tenant_slug, agent_slug, query, policy):
            captured["policy"] = policy
            return await super().retrieve(tenant_slug, agent_slug, query, policy)

    retrieval = CapturingRetrievalRepo(raw=_raw_context())
    provider = CacheAsideKnowledgeProvider(availability, retrieval)

    policy = RetrievalPolicy(top_k=3, max_tokens=200, minimum_score=0.5)
    await provider.retrieve("acme", "sup", "q", policy=policy)

    assert captured["policy"] is policy


async def test_close_delegates_to_both_repositories():
    availability, retrieval = FakeAvailabilityRepo(), FakeRetrievalRepo()
    provider = CacheAsideKnowledgeProvider(availability, retrieval)
    await provider.close()
    assert availability.closed and retrieval.closed
