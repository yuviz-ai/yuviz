"""Read-only Redis repository over the keys Config Service writes; never writes."""

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
        # A Redis outage is just a miss; the caller falls through to HTTP.
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
