"""Read-only Redis repository over the keys Config Service writes; never writes."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis

from ..exceptions import RepositoryUnavailableError

log = logging.getLogger(__name__)

# Read on the call path: a hung Redis must fail fast (Gateway uses 100 ms too).
_TIMEOUT_S = 0.1


class RedisConfigRepository:
    def __init__(self, redis_url: str) -> None:
        self._client = redis.from_url(
            redis_url, decode_responses=True,
            socket_timeout=_TIMEOUT_S, socket_connect_timeout=_TIMEOUT_S,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(key)
        except redis.RedisError as exc:
            log.warning("RedisConfigRepository: Redis unreachable key=%s", key)
            raise RepositoryUnavailableError(f"redis unavailable key={key}") from exc
        return json.loads(raw) if raw is not None else None

    async def fetch_tenant(self, tenant_slug: str) -> dict[str, Any] | None:
        return await self._get_json(f"tenant:{tenant_slug}")

    async def fetch_agent(self, tenant_slug: str, agent_slug: str) -> dict[str, Any] | None:
        return await self._get_json(f"agent:{tenant_slug}:{agent_slug}")

    async def fetch_provider_config(self, provider_id: str) -> dict[str, Any] | None:
        return await self._get_json(f"provider:{provider_id}")

    async def fetch_call_flow(self, tenant_slug: str, call_flow_id: str) -> dict[str, Any] | None:
        return await self._get_json(f"callflow:{tenant_slug}:{call_flow_id}")
