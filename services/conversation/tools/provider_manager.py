"""ToolProviderManager — creates and caches tool provider instances per tool_provider_config.id.
Mirrors AIProviderManager; secrets are resolved once at construction."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from typing import Any, Awaitable, Callable

from ..secret_resolver import SecretResolver
from .policy_resolver import ResolvedToolPolicy

log = logging.getLogger(__name__)

ProviderFactory = Callable[[ResolvedToolPolicy, str | None], Awaitable[Any]]


async def _make_toolexec(policy: ResolvedToolPolicy, api_key: str | None) -> Any:
    from .providers.toolexec.client import ToolExecClient

    # Internal engine: authenticates with the service account, never a tenant api_key_ref.
    return ToolExecClient(
        base_url=os.environ.get("TOOLEXEC_SERVICE_URL", "http://localhost:8600"),
        auth_base_url=os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000"),
        service_email=os.environ.get("CONFIG_SERVICE_EMAIL", ""),
        service_password=os.environ.get("CONFIG_SERVICE_PASSWORD", ""),
    )


_DEFAULT_REGISTRY: dict[str, ProviderFactory] = {
    "toolexec": _make_toolexec,
}


class ToolProviderManager:
    def __init__(
        self, secret_resolver: SecretResolver, registry: dict[str, ProviderFactory] | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._registry = dict(_DEFAULT_REGISTRY) if registry is None else dict(registry)
        self._instances: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, policy: ResolvedToolPolicy) -> Any:
        cached = self._instances.get(policy.tool_provider_config_id)
        if cached is not None:
            return cached

        async with self._locks[policy.tool_provider_config_id]:
            cached = self._instances.get(policy.tool_provider_config_id)
            if cached is not None:
                return cached

            factory = self._registry.get(policy.engine)
            if factory is None:
                raise ValueError(f"no tool provider factory registered for engine={policy.engine!r}")

            api_key = await self._secret_resolver.resolve(policy.api_key_ref) if policy.api_key_ref else None
            instance = await factory(policy, api_key)
            self._instances[policy.tool_provider_config_id] = instance
            return instance

    def cached_ids(self) -> frozenset[str]:
        return frozenset(self._instances.keys())
