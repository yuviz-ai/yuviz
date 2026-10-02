"""Lazy singletons for routers; not on app.state because ASGITransport tests skip lifespan."""

from __future__ import annotations

from .embedding_manager import EmbeddingProviderManager
from .secret_resolver import CompositeSecretResolver
from .vector_repository import PgVectorRepository

_vector_repo: PgVectorRepository | None = None
_embedding_manager: EmbeddingProviderManager | None = None


async def get_vector_repo() -> PgVectorRepository:
    global _vector_repo
    if _vector_repo is None:
        _vector_repo = PgVectorRepository()
    return _vector_repo


def get_embedding_manager() -> EmbeddingProviderManager:
    global _embedding_manager
    if _embedding_manager is None:
        _embedding_manager = EmbeddingProviderManager(CompositeSecretResolver())
    return _embedding_manager
