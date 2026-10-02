"""DID -> tenant/agent from Redis "did:{did}" (same key as the Gateway's PhoneRoute).

Hot call path: Redis only, never Config Service/Postgres.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import redis.asyncio as redis

log = logging.getLogger(__name__)

DEFAULT_TENANT = "default"
DEFAULT_AGENT = "default"

_client: redis.Redis | None = None


def _timeout_s() -> float:
    return int(os.environ.get("DID_REDIS_TIMEOUT_MS", "250")) / 1000.0


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        timeout = _timeout_s()
        _client = redis.from_url(
            url, decode_responses=True,
            socket_timeout=timeout, socket_connect_timeout=timeout,
            retry_on_timeout=False,
        )
    return _client


async def resolve_did_route(did: str) -> tuple[str, str] | None:
    """(tenant_slug, agent_slug), or None for any non-hit (miss, malformed, timeout, down)."""
    try:
        raw = await asyncio.wait_for(_get_client().get(f"did:{did}"), _timeout_s())
    except (redis.RedisError, asyncio.TimeoutError):
        log.warning("resolve_did_route: Redis unreachable/timed out did=%s", did)
        return None

    if raw is None:
        log.info("resolve_did_route: no route for did=%s", did)
        return None

    try:
        route = json.loads(raw)
        return route["tenant_slug"], route["agent_slug"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("resolve_did_route: malformed route did=%s raw=%r", did, raw)
        return None


async def resolve_did(did: str) -> tuple[str, str]:
    """Legacy: falls back to default tenant/agent. Prefer resolve_did_route (distinguishes misses)."""
    return await resolve_did_route(did) or (DEFAULT_TENANT, DEFAULT_AGENT)
