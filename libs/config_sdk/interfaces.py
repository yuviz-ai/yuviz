"""
Two abstraction layers, deliberately not one:

IConfigProvider — business-level, the ONLY thing Conversation Service (or
any future consumer) depends on. Speaks in Tenant/Agent/ProviderConfig/
RuntimeConfig — never mentions Redis, HTTP, or Postgres. get_runtime_config()
is the primary method real callers use (one call, one immutable snapshot per
session); the individual get_tenant()/get_agent()/get_provider_config() stay
available as lower-level primitives for a future consumer that only needs
one piece (e.g. a Notification Service that only ever needs get_tenant()).

IConfigRepository — transport-level, internal to this package. A raw-dict
fetch-by-key contract that RedisConfigRepository and HttpConfigRepository
each implement. CacheAsideConfigProvider (the production IConfigProvider
implementation) composes two IConfigRepository instances and does the
fallback dance + dict-to-DTO mapping — it never constructs a redis-py or
httpx client itself, only receives repositories via its constructor. This is
what "the SDK exposes abstractions rather than transport details" means
concretely: swapping HttpConfigRepository for, say, a future gRPC-based
repository touches one class, not CacheAsideConfigProvider's orchestration
logic or anything upstream of it.
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import Agent, CallFlow, Prompt, ProviderConfig, RuntimeConfig, Tenant, ToolSpec


class IConfigProvider(Protocol):
    async def get_runtime_config(self, tenant_slug: str, agent_slug: str) -> RuntimeConfig | None: ...

    async def get_tenant(self, tenant_slug: str) -> Tenant | None: ...

    async def get_agent(self, tenant_slug: str, agent_slug: str) -> Agent | None: ...

    async def get_provider_config(self, provider_id: str) -> ProviderConfig | None: ...

    async def get_prompt(self, tenant_slug: str, agent_slug: str) -> Prompt | None: ...

    async def get_voice(self, tenant_slug: str, agent_slug: str) -> str | None: ...

    async def get_tools(self, tenant_slug: str, agent_slug: str) -> list[ToolSpec]: ...

    async def get_call_flow(self, tenant_slug: str, call_flow_id: str) -> CallFlow | None: ...

    async def close(self) -> None: ...


class IConfigRepository(Protocol):
    """Raw dict in, raw dict out — no DTOs here. Mapping to typed models is
    CacheAsideConfigProvider's job, not a repository's, so a repository
    implementation never needs to import models.py at all."""

    async def fetch_tenant(self, tenant_slug: str) -> dict[str, Any] | None: ...

    async def fetch_agent(self, tenant_slug: str, agent_slug: str) -> dict[str, Any] | None: ...

    async def fetch_provider_config(self, provider_id: str) -> dict[str, Any] | None: ...

    async def fetch_call_flow(self, tenant_slug: str, call_flow_id: str) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...
