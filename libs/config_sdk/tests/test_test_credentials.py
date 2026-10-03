"""Runs against real local Redis, same convention as test_redis_repository.py."""

from __future__ import annotations

import asyncio
import os
import uuid

import redis.asyncio as redis

from libs.config_sdk.test_credentials import (
    CHAT_TTL_S,
    VOICE_TTL_S,
    TestGrant,
    mint_test_credential,
    redeem_test_credential,
)

REDIS_URL = os.environ["REDIS_URL"]


def _grant(channel="voice", session_id=None) -> TestGrant:
    return TestGrant(
        tenant_slug=f"t-{uuid.uuid4().hex[:8]}", tenant_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()), agent_slug="reception", channel=channel, session_id=session_id,
    )


async def _client():
    return redis.from_url(REDIS_URL, decode_responses=True)


async def test_ttl_constants():
    assert (VOICE_TTL_S, CHAT_TTL_S) == (60, 900)


async def test_key_is_sha256_and_plaintext_is_in_no_key():
    import hashlib
    client = await _client()
    grant = _grant()
    token = await mint_test_credential(client, grant, VOICE_TTL_S)
    key = f"testcred:{hashlib.sha256(token.encode()).hexdigest()}"
    try:
        assert await client.exists(key) == 1
        assert 0 < await client.ttl(key) <= VOICE_TTL_S
        assert not [k async for k in client.scan_iter(match=f"*{token}*")]
    finally:
        await client.delete(key)
        await client.aclose()


async def test_consume_true_redeems_once():
    client = await _client()
    grant = _grant()
    token = await mint_test_credential(client, grant, VOICE_TTL_S)
    try:
        assert await redeem_test_credential(client, token, channel="voice", consume=True) == grant
        assert await redeem_test_credential(client, token, channel="voice", consume=True) is None
    finally:
        await client.aclose()


class _GetBarrier:
    """Holds every GET result until all n callers have read, so each caller
    has seen the record before any DEL runs."""

    def __init__(self, client, n):
        self._client, self._n, self._seen, self._all_read = client, n, 0, asyncio.Event()

    async def get(self, key):
        value = await self._client.get(key)
        self._seen += 1
        if self._seen == self._n:
            self._all_read.set()
        await self._all_read.wait()
        return value

    async def delete(self, key):
        return await self._client.delete(key)


async def test_concurrent_consume_has_one_winner():
    client = await _client()
    token = await mint_test_credential(client, _grant(), VOICE_TTL_S)
    barrier = _GetBarrier(client, 8)
    try:
        results = await asyncio.gather(*[
            redeem_test_credential(barrier, token, channel="voice", consume=True) for _ in range(8)
        ])
        assert len([r for r in results if r is not None]) == 1
    finally:
        await client.aclose()


async def test_channel_mismatch_returns_none_and_does_not_consume():
    client = await _client()
    grant = _grant("voice")
    token = await mint_test_credential(client, grant, VOICE_TTL_S)
    try:
        assert await redeem_test_credential(client, token, channel="chat", consume=True) is None
        assert await redeem_test_credential(client, token, channel="voice", consume=True) == grant
    finally:
        await client.aclose()


async def test_corrupt_record_returns_none():
    client = await _client()
    token = await mint_test_credential(client, _grant(), VOICE_TTL_S)
    import hashlib
    key = f"testcred:{hashlib.sha256(token.encode()).hexdigest()}"
    try:
        await client.set(key, "{not json", ex=VOICE_TTL_S)
        assert await redeem_test_credential(client, token, channel="voice", consume=True) is None
    finally:
        await client.delete(key)
        await client.aclose()


async def test_consume_false_repeats_until_key_expires():
    import hashlib
    client = await _client()
    grant = _grant("chat", session_id="s1")
    token = await mint_test_credential(client, grant, CHAT_TTL_S)
    key = f"testcred:{hashlib.sha256(token.encode()).hexdigest()}"
    try:
        for _ in range(3):
            assert await redeem_test_credential(client, token, channel="chat", consume=False) == grant
        await client.pexpire(key, 1)
        await asyncio.sleep(0.05)
        assert await redeem_test_credential(client, token, channel="chat", consume=False) is None
    finally:
        await client.aclose()


async def test_unknown_token_returns_none():
    client = await _client()
    try:
        assert await redeem_test_credential(client, "nope", channel="voice", consume=True) is None
    finally:
        await client.aclose()
