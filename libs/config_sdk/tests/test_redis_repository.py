"""Runs against real local Redis, same convention as the rest of this repo's
tests-that-touch-infra (see services/config/tests/conftest.py)."""

from __future__ import annotations

import json
import os
import uuid

import redis.asyncio as redis

from libs.config_sdk.repositories.redis_repository import RedisConfigRepository

REDIS_URL = os.environ["REDIS_URL"]


async def test_fetch_tenant_hit():
    slug = f"test-{uuid.uuid4().hex[:8]}"
    client = redis.from_url(REDIS_URL, decode_responses=True)
    await client.set(f"tenant:{slug}", json.dumps({"id": "t1", "slug": slug, "name": "Test"}))

    repo = RedisConfigRepository(REDIS_URL)
    result = await repo.fetch_tenant(slug)
    assert result == {"id": "t1", "slug": slug, "name": "Test"}

    await client.delete(f"tenant:{slug}")
    await client.aclose()
    await repo.close()


async def test_fetch_tenant_miss_returns_none():
    repo = RedisConfigRepository(REDIS_URL)
    result = await repo.fetch_tenant(f"no-such-tenant-{uuid.uuid4().hex[:8]}")
    assert result is None
    await repo.close()


async def test_fetch_agent_uses_correct_key_format():
    tenant_slug = f"test-{uuid.uuid4().hex[:8]}"
    agent_slug = "reception"
    client = redis.from_url(REDIS_URL, decode_responses=True)
    await client.set(f"agent:{tenant_slug}:{agent_slug}", json.dumps({"id": "a1", "slug": agent_slug}))

    repo = RedisConfigRepository(REDIS_URL)
    result = await repo.fetch_agent(tenant_slug, agent_slug)
    assert result == {"id": "a1", "slug": agent_slug}

    await client.delete(f"agent:{tenant_slug}:{agent_slug}")
    await client.aclose()
    await repo.close()


async def test_fetch_provider_config_uses_correct_key_format():
    provider_id = str(uuid.uuid4())
    client = redis.from_url(REDIS_URL, decode_responses=True)
    await client.set(f"provider:{provider_id}", json.dumps({"id": provider_id, "engine": "deepgram"}))

    repo = RedisConfigRepository(REDIS_URL)
    result = await repo.fetch_provider_config(provider_id)
    assert result == {"id": provider_id, "engine": "deepgram"}

    await client.delete(f"provider:{provider_id}")
    await client.aclose()
    await repo.close()


async def test_fetch_call_flow_uses_correct_key_format():
    tenant_slug = f"test-{uuid.uuid4().hex[:8]}"
    call_flow_id = str(uuid.uuid4())
    client = redis.from_url(REDIS_URL, decode_responses=True)
    await client.set(
        f"callflow:{tenant_slug}:{call_flow_id}",
        json.dumps({"id": call_flow_id, "tenant_slug": tenant_slug}),
    )

    repo = RedisConfigRepository(REDIS_URL)
    result = await repo.fetch_call_flow(tenant_slug, call_flow_id)
    assert result == {"id": call_flow_id, "tenant_slug": tenant_slug}

    await client.delete(f"callflow:{tenant_slug}:{call_flow_id}")
    await client.aclose()
    await repo.close()
