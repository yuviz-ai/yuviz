"""
Short-lived credentials that let an admin test an inactive agent.

Config mints one (bound to tenant, agent and channel) and stores it in
Redis; Conversation (voice) and Config's chat-turn route (chat) redeem it.
Only the sha256 of the token is used in the key, so the plaintext never
appears in Redis.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from typing import Literal

VOICE_TTL_S = 60
CHAT_TTL_S = 900


@dataclass(frozen=True)
class TestGrant:
    __test__ = False  # not a pytest class

    tenant_slug: str
    tenant_id: str
    agent_id: str
    agent_slug: str
    channel: Literal["voice", "chat"]
    session_id: str | None


def _key(token: str) -> str:
    return f"testcred:{hashlib.sha256(token.encode()).hexdigest()}"


async def mint_test_credential(redis, grant: TestGrant, ttl_s: int) -> str:
    token = secrets.token_urlsafe(32)
    await redis.set(_key(token), json.dumps(asdict(grant)), ex=ttl_s)
    return token


async def redeem_test_credential(
    redis, token: str, *, channel: Literal["voice", "chat"], consume: bool,
) -> TestGrant | None:
    """Returns the grant, or None when the token is unknown, expired, corrupt
    or minted for another channel. A channel mismatch never consumes the
    credential. With consume=True exactly one caller wins the DEL."""
    key = _key(token)
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        grant = TestGrant(**json.loads(raw))
    except (ValueError, TypeError):
        return None
    if grant.channel != channel:
        return None
    if consume and await redis.delete(key) != 1:
        return None
    return grant
