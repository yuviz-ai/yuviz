"""
Evicts cached provider instances when Config Service publishes a provider_config change.
Runs as its own task with no shared locks, so a slow Redis never adds call latency.
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis

from .ai_provider_manager import AIProviderManager

log = logging.getLogger(__name__)

CHANNEL = "provider_config_changed"


class ProviderConfigSubscriber:
    def __init__(self, redis_url: str, provider_manager: AIProviderManager) -> None:
        self._redis_url = redis_url
        self._provider_manager = provider_manager
        self._stopped = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ProviderConfigSubscriber: connection lost, reconnecting in 5s")
                await asyncio.sleep(5)

    async def _listen_once(self) -> None:
        client = redis.from_url(self._redis_url, decode_responses=True)
        try:
            pubsub = client.pubsub()
            await pubsub.subscribe(CHANNEL)
            log.info("ProviderConfigSubscriber: subscribed to %s", CHANNEL)
            async for message in pubsub.listen():
                if self._stopped:
                    break
                if message.get("type") != "message":
                    continue  # the subscribe confirmation itself arrives as a "subscribe" message
                config_id = message["data"]
                evicted = self._provider_manager.invalidate(config_id)
                if evicted:
                    log.info(
                        "ProviderConfigSubscriber: invalidated cached provider config_id=%s — "
                        "next call using it will reconstruct from the new config",
                        config_id,
                    )
        finally:
            await client.aclose()
