"""
Redis cache-aside helpers for Config Service. Write paths must invalidate()
the keys they touch; never cache resolved secrets.

A Redis outage degrades to reading Postgres — errors are never propagated.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis.asyncio as redis

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60

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


async def get_json(key: str) -> dict[str, Any] | None:
    try:
        raw = await get_client().get(key)
    except redis.RedisError:
        log.warning("cache.get_json: Redis unreachable, treating as cache miss key=%s", key)
        return None
    return json.loads(raw) if raw is not None else None


async def set_json(key: str, value: dict[str, Any], ttl: int | None = DEFAULT_TTL_SECONDS) -> None:
    # default=str: UUID/datetime come back as strings on read.
    # ttl=None: no expiry, for keys every write path rewrites (DID routing cache).
    try:
        if ttl is None:
            await get_client().set(key, json.dumps(value, default=str))
        else:
            await get_client().set(key, json.dumps(value, default=str), ex=ttl)
    except redis.RedisError:
        log.warning("cache.set_json: Redis unreachable, skipping cache populate key=%s", key)


async def invalidate(*keys: str) -> None:
    if not keys:
        return
    try:
        await get_client().delete(*keys)
    except redis.RedisError:
        log.warning("cache.invalidate: Redis unreachable, stale entries will expire via TTL keys=%s", keys)


async def publish(channel: str, message: str) -> None:
    """Pub/Sub invalidation (e.g. provider_config_changed); skipped if Redis is down."""
    try:
        await get_client().publish(channel, message)
    except redis.RedisError:
        log.warning("cache.publish: Redis unreachable, subscribers will not be notified channel=%s", channel)
