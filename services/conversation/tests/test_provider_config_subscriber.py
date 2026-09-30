"""
Tests the real cross-process contract against a real local Redis (the same
one Config Service's cache.py publishes to) — not a mocked pub/sub client.
See provider_config_subscriber.py's module docstring for why this exists.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import redis.asyncio as redis

from ..ai_provider_manager import AIProviderManager, ProviderConfig
from ..provider_config_subscriber import CHANNEL, ProviderConfigSubscriber

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


class FakeProviderInstance:
    def __init__(self, cfg: ProviderConfig, api_key: str | None) -> None:
        self.cfg = cfg


async def _fake_factory(cfg: ProviderConfig, api_key: str | None) -> FakeProviderInstance:
    return FakeProviderInstance(cfg, api_key)


class FakeSecretResolver:
    async def resolve(self, ref: str) -> str:
        return f"resolved:{ref}"


FAKE_REGISTRY = {("stt", "fake_engine"): _fake_factory}


async def test_subscriber_invalidates_manager_on_published_message():
    manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
    cfg = ProviderConfig(id="p1", role="stt", engine="fake_engine")
    await manager.get(cfg)
    assert manager.cached_ids() == {"p1"}

    subscriber = ProviderConfigSubscriber(REDIS_URL, manager)
    subscriber.start()
    try:
        # Give the subscriber's background task time to actually connect
        # and subscribe before publishing — otherwise the message could be
        # published before anyone is listening for it.
        await asyncio.sleep(0.3)

        publisher = redis.from_url(REDIS_URL, decode_responses=True)
        await publisher.publish(CHANNEL, "p1")
        await publisher.aclose()

        for _ in range(20):  # up to ~2s
            if manager.cached_ids() == frozenset():
                break
            await asyncio.sleep(0.1)

        assert manager.cached_ids() == frozenset()
    finally:
        await subscriber.stop()


async def test_subscriber_ignores_messages_for_configs_it_never_cached():
    manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
    cfg = ProviderConfig(id="p1", role="stt", engine="fake_engine")
    await manager.get(cfg)

    subscriber = ProviderConfigSubscriber(REDIS_URL, manager)
    subscriber.start()
    try:
        await asyncio.sleep(0.3)

        publisher = redis.from_url(REDIS_URL, decode_responses=True)
        await publisher.publish(CHANNEL, "some-other-config-id")
        await publisher.aclose()
        await asyncio.sleep(0.5)

        assert manager.cached_ids() == {"p1"}  # untouched
    finally:
        await subscriber.stop()


async def test_subscriber_stop_cancels_cleanly():
    manager = AIProviderManager(FakeSecretResolver(), registry=FAKE_REGISTRY)
    subscriber = ProviderConfigSubscriber(REDIS_URL, manager)
    subscriber.start()
    await asyncio.sleep(0.2)
    await subscriber.stop()  # must not raise or hang
