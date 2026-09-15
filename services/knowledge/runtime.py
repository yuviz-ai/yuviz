"""
Lazy module-level singletons for this service's own heavier objects —
same convention as db.get_pool()/cache.get_client(): constructed on first
use, not via FastAPI's lifespan state. Kept in their own module (not
app.py) so routers can import these getters without a circular import on
app.py, which itself imports the routers.

This matters beyond style: httpx.ASGITransport (used by this service's and
the SDK's own tests) never runs the lifespan context, so anything a route
depends on must be reachable without it — exactly like every router in
this codebase already calls db.get_pool()/cache.get_client() directly
rather than reading app.state.
"""

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
