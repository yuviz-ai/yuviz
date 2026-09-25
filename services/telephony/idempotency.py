"""
Idempotency over Redis, tenant-scoped by construction — every function
takes tenant_id required and positional-second, through one private
_key() helper no caller bypasses (finding #3: two tenants minting the
identical key must address different Redis entries).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

import redis.asyncio as redis

log = logging.getLogger("telephony.idempotency")

IDEM_TTL_S = 120
POLL_BUDGET_S = 10.0
POLL_INTERVAL_S = 0.2

_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _client = redis.from_url(url, decode_responses=True)
    return _client


def _key(prefix: str, provider: str, tenant_id: uuid.UUID, key: str) -> str:
    return f"{prefix}:{provider}:{tenant_id}:{key}"


async def claim(provider: str, tenant_id: uuid.UUID, key: str) -> bool:
    """SET NX EX 120 "in_flight" — True means this call won the claim."""
    redis_key = _key("idem", provider, tenant_id, key)
    try:
        won = await _get_client().set(
            redis_key, json.dumps({"state": "in_flight"}), nx=True, ex=IDEM_TTL_S,
        )
    except redis.RedisError:
        log.warning("idempotency.claim: Redis unreachable key=%s", redis_key)
        return False
    return bool(won)


async def finalize(provider: str, tenant_id: uuid.UUID, key: str, outcome: dict[str, Any]) -> None:
    redis_key = _key("idem", provider, tenant_id, key)
    try:
        await _get_client().set(redis_key, json.dumps(outcome), ex=IDEM_TTL_S)
    except redis.RedisError:
        log.warning("idempotency.finalize: Redis unreachable key=%s", redis_key)


async def read(provider: str, tenant_id: uuid.UUID, key: str) -> dict[str, Any] | None:
    """No poll, no claim — a plain read of whatever's currently stored."""
    redis_key = _key("idem", provider, tenant_id, key)
    try:
        raw = await _get_client().get(redis_key)
    except redis.RedisError:
        log.warning("idempotency.read: Redis unreachable key=%s", redis_key)
        return None
    return json.loads(raw) if raw is not None else None


async def await_outcome(provider: str, tenant_id: uuid.UUID, key: str) -> dict[str, Any] | None:
    """Bounded poll for a final (non in_flight) entry. Returns None only on
    budget exhaustion, never on error — a Redis failure during the poll is
    logged and also returns None, so the caller turns it into 202. There is
    no branch in which a losing claimant reaches the vendor."""
    deadline = time.monotonic() + POLL_BUDGET_S
    while True:
        entry = await read(provider, tenant_id, key)
        if entry is not None and entry.get("state") != "in_flight":
            return entry
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(POLL_INTERVAL_S)


async def note_reference(provider: str, tenant_id: uuid.UUID, key: str, call_id: str) -> None:
    redis_key = _key("idemref", provider, tenant_id, key)
    try:
        await _get_client().set(redis_key, call_id, ex=IDEM_TTL_S)
    except redis.RedisError:
        log.warning("idempotency.note_reference: Redis unreachable key=%s", redis_key)


async def observed_call_id(provider: str, tenant_id: uuid.UUID, key: str) -> str | None:
    redis_key = _key("idemref", provider, tenant_id, key)
    try:
        return await _get_client().get(redis_key)
    except redis.RedisError:
        log.warning("idempotency.observed_call_id: Redis unreachable key=%s", redis_key)
        return None
