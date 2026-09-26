"""
DID -> tenant/agent resolution, read directly from Redis — mirrors the
Gateway's own PhoneRoute::from_redis() exactly (gateway/include/telephony),
same "did:{did}" key shape. This sits on the real-time call path (a real
inbound call is waiting on this lookup before audio can start), so it
follows the platform's hot-path rule: Redis only, never Config
Service/Postgres here — see project memory "architecture_decisions_voiceai"
and "phase5_coding_rules".

Moved here from services/vobiz/redis_route.py so Cloudonix reads the same
cache key the Gateway and Vobiz read, rather than a second DID reader
(PRD constraint). `resolve_did_route()` is new: it distinguishes a miss
from a hit, which `resolve_did()` (kept for Vobiz's existing "always fall
back to the default tenant" behavior) cannot — and Cloudonix's tenant
boundary needs that distinction, because a route to a tenant whose slug
is literally "default" must not be confused with "no route at all" (see
.sdlc/cloudonix-telephony-provider/02-design.md's Interfaces section).
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
    """Returns (tenant_slug, agent_slug), or None for every non-hit — miss,
    malformed JSON, timeout, unreachable. `asyncio.wait_for` is a hard
    ceiling independent of redis-py's own retry/health-check internals.
    `redis.TimeoutError` is a `RedisError` subclass, so slow and
    unreachable collapse into the same branch by construction — the
    caller cannot tell them apart, only the log does."""
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
    """Returns (tenant_slug, agent_slug). An unrecognized DID (never
    provisioned, or Redis unreachable) resolves to the default tenant/agent
    — same "never a rejected call" posture as the Gateway, never an
    exception on the call path. Back-compat wrapper services/vobiz/app.py
    depends on; Cloudonix must call resolve_did_route directly instead —
    this wrapper cannot distinguish a miss from a genuine route to a
    tenant whose slug is literally "default"."""
    return await resolve_did_route(did) or (DEFAULT_TENANT, DEFAULT_AGENT)
