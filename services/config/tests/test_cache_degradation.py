"""A Redis outage must never propagate out of the cache helpers; reads degrade to Postgres.

Tests temporarily swap the shared cache._client for an unreachable one and restore it.
"""

from __future__ import annotations

import redis.asyncio as redis

from services.config import cache


async def _with_unreachable_client(coro_factory):
    original = cache._client
    cache._client = redis.from_url("redis://localhost:9999/0", decode_responses=True)
    try:
        return await coro_factory()
    finally:
        await cache._client.aclose()
        cache._client = original


async def test_get_json_returns_none_on_redis_outage_instead_of_raising():
    result = await _with_unreachable_client(lambda: cache.get_json("some:key"))
    assert result is None


async def test_set_json_does_not_raise_on_redis_outage():
    # No exception is the assertion — a raised RedisError would fail the test.
    await _with_unreachable_client(lambda: cache.set_json("some:key", {"a": 1}))


async def test_invalidate_does_not_raise_on_redis_outage():
    await _with_unreachable_client(lambda: cache.invalidate("some:key"))


async def test_publish_does_not_raise_on_redis_outage():
    await _with_unreachable_client(lambda: cache.publish("some_channel", "some message"))
