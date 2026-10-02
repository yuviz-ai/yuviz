"""Knowledge SDK protocols.

Redis answers only the cheap "any enabled KB?" pre-check; retrieval always goes over HTTP.
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import RetrievalPolicy, RetrievedContext


class IKnowledgeProvider(Protocol):
    async def retrieve(
        self,
        tenant_slug: str,
        agent_slug: str,
        query: str,
        policy: RetrievalPolicy | None = None,
    ) -> RetrievedContext | None:
        """None when no eligible KB or the retrieval plane is down; never raises."""
        ...

    async def close(self) -> None: ...


class IKnowledgeAvailabilityRepository(Protocol):
    """Cheap Redis pre-check for any enabled KB; read-only (Knowledge Service writes it)."""

    async def has_enabled_kb(self, tenant_slug: str, agent_slug: str) -> bool | None:
        """None = cache miss (unknown, not false)."""
        ...

    async def close(self) -> None: ...


class IRetrievalRepository(Protocol):
    """Raw dicts only; mapping to RetrievedContext is the provider's job."""

    async def retrieve(
        self,
        tenant_slug: str,
        agent_slug: str,
        query: str,
        policy: RetrievalPolicy,
    ) -> dict[str, Any] | None: ...

    async def has_enabled_kb(self, tenant_slug: str, agent_slug: str) -> bool: ...

    async def close(self) -> None: ...
