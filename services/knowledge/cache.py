"""Redis client for Knowledge Service. Write-through, no TTL: agent_kb:{tenant}:{agent} -> "1"/"0".
A Redis outage degrades to a cache miss (fall back to Postgres)."""

from __future__ import annotations

import logging
import os

import redis.asyncio as redis

log = logging.getLogger(__name__)

_client: redis.Redis | None = None


def get_client(url: str | None = None) -> redis.Redis:
    global _client
    if _client is None:
        url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _client = redis.from_url(url, decode_responses=True)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _agent_kb_key(tenant_slug: str, agent_slug: str) -> str:
    return f"agent_kb:{tenant_slug}:{agent_slug}"


async def set_has_enabled_kb(tenant_slug: str, agent_slug: str, enabled: bool) -> None:
    try:
        await get_client().set(_agent_kb_key(tenant_slug, agent_slug), "1" if enabled else "0")
    except redis.RedisError:
        log.warning(
            "cache.set_has_enabled_kb: Redis unreachable, skipping write "
            "tenant=%s agent=%s", tenant_slug, agent_slug,
        )


async def get_has_enabled_kb(tenant_slug: str, agent_slug: str) -> bool | None:
    try:
        raw = await get_client().get(_agent_kb_key(tenant_slug, agent_slug))
    except redis.RedisError:
        log.warning(
            "cache.get_has_enabled_kb: Redis unreachable, treating as miss "
            "tenant=%s agent=%s", tenant_slug, agent_slug,
        )
        return None
    return None if raw is None else raw == "1"
