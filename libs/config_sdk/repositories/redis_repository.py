"""
RedisConfigRepository — read-only, raw-dict, same key formats Config
Service's own tenants.py/agents.py/provider_configs.py already write
(tenant:{slug}, agent:{tenant_slug}:{agent_slug}, provider:{id}). This
repository never writes to Redis — see providers/cache_aside.py's docstring
for why the write-back on a miss is deliberately left to Config Service's
existing REST handlers rather than duplicated here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis

log = logging.getLogger(__name__)


class RedisConfigRepository:
    def __init__(self, redis_url: str) -> None:
        self._client = redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(self, key: str) -> dict[str, Any] | None:
        # Same degrade-on-any-failure contract as services/config/cache.py's
        # get_json() — a Redis outage here is just a permanent miss, not an
        # error the caller needs to handle specially; CacheAsideConfigProvider
        # falls through to HTTP either way.
        try:
            raw = await self._client.get(key)
        except redis.RedisError:
            log.warning("RedisConfigRepository: Redis unreachable, treating as miss key=%s", key)
            return None
        return json.loads(raw) if raw is not None else None

    async def fetch_tenant(self, tenant_slug: str) -> dict[str, Any] | None:
        return await self._get_json(f"tenant:{tenant_slug}")

    async def fetch_agent(self, tenant_slug: str, agent_slug: str) -> dict[str, Any] | None:
        return await self._get_json(f"agent:{tenant_slug}:{agent_slug}")

    async def fetch_provider_config(self, provider_id: str) -> dict[str, Any] | None:
        return await self._get_json(f"provider:{provider_id}")

    async def fetch_call_flow(self, tenant_slug: str, call_flow_id: str) -> dict[str, Any] | None:
        return await self._get_json(f"callflow:{tenant_slug}:{call_flow_id}")
