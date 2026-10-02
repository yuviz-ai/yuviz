"""Knowledge SDK: the only component that knows where retrieved context comes from.

Consumers depend on IKnowledgeProvider and these models, never on Redis/HTTP directly.
"""

from .interfaces import IKnowledgeAvailabilityRepository, IKnowledgeProvider, IRetrievalRepository
from .models import ChunkSource, RetrievalPolicy, RetrievedChunk, RetrievedContext
from .providers import CacheAsideKnowledgeProvider, MockKnowledgeProvider
from .repositories import HttpKnowledgeRepository, RedisKnowledgeRepository

__all__ = [
    "IKnowledgeProvider",
    "IKnowledgeAvailabilityRepository",
    "IRetrievalRepository",
    "RetrievalPolicy",
    "ChunkSource",
    "RetrievedChunk",
    "RetrievedContext",
    "CacheAsideKnowledgeProvider",
    "MockKnowledgeProvider",
    "RedisKnowledgeRepository",
    "HttpKnowledgeRepository",
]
