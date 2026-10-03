"""
Credential branch of the handler factory's agent resolution. Runs against
real local Redis (credentials) and a MockConfigProvider (agents); the
legacy fallback is spied, never executed.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import redis.asyncio as aioredis

from libs.config_sdk.providers.mock_provider import MockConfigProvider
from libs.config_sdk.test_credentials import (
    CHAT_TTL_S,
    VOICE_TTL_S,
    TestGrant,
    mint_test_credential,
    _key,
)

from .. import __main__ as entry
from ..generated.voiceai.v1 import conversation_pb2 as pb
from ..session import AgentUnavailable, SessionContext
from .test_agent_resolver import _registry

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SENTINEL = "sentinel-credential-9f3a"
LEGACY = ("legacy-runtime-config", "legacy-bundle")


@pytest.fixture
async def credential_redis():
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield client
    async for key in client.scan_iter("testcred:*"):
        await client.delete(key)
    await client.aclose()


@pytest.fixture
def legacy_calls(monkeypatch):
    """Slugs passed to the legacy load_agent; to_runtime_config is stubbed."""
    loaded: list[str] = []
    monkeypatch.setattr(entry, "load_agent", lambda script_id: loaded.append(script_id) or "agent")
    monkeypatch.setattr(entry, "to_runtime_config", lambda *a: LEGACY)
    return loaded


def _config(status: str = "inactive", agent_id: str = "agent-1") -> MockConfigProvider:
    mock = MockConfigProvider()
    mock.add_tenant(
        slug="acme", name="Acme",
        default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
    )
    mock.add_agent(
        "acme", slug="sup", name="Sup", id=agent_id, status=status,
        greeting="Hi there", system_prompt="Be helpful.",
    )
    mock.add_provider_config(id="stt1", role="stt", engine="fake_stt")
    mock.add_provider_config(id="llm1", role="llm", engine="fake_llm")
    mock.add_provider_config(id="tts1", role="tts", engine="fake_tts", voice="aria")
    return mock


def _grant(**overrides) -> TestGrant:
    fields = dict(
        tenant_slug="acme", tenant_id="tenant-id", agent_id="agent-1", agent_slug="sup",
        channel="voice", session_id=None,
    )
    return TestGrant(**{**fields, **overrides})


def _ctx(credential: str = "", *, direction: str = "test", tenant="acme", slug="sup") -> SessionContext:
    return SessionContext(
        session_id="s1", tenant_id=tenant, direction=direction, script_id=slug,
        test_credential=credential,
    )


async def _resolve(ctx, redis_client, config):
    return await entry._resolve_session_deps(ctx, redis_client, _registry(), config, None, None, None)


async def test_inactive_agent_with_voice_credential_gets_its_prompt_and_voice(credential_redis, legacy_calls):
    token = await mint_test_credential(credential_redis, _grant(), VOICE_TTL_S)

    runtime_config, _bundle = await _resolve(_ctx(token), credential_redis, _config("inactive"))

    assert runtime_config.agent.status == "inactive"
    assert runtime_config.conversation.system_prompt == "Be helpful."
    assert runtime_config.media.voice == "aria"
    assert legacy_calls == []


async def test_credential_redeems_once(credential_redis, legacy_calls):
    token = await mint_test_credential(credential_redis, _grant(), VOICE_TTL_S)
    config = _config("inactive")

    await _resolve(_ctx(token), credential_redis, config)
    with pytest.raises(AgentUnavailable):
        await _resolve(_ctx(token), credential_redis, config)
    assert legacy_calls == []


async def _expired_token(client) -> str:
    token = await mint_test_credential(client, _grant(), VOICE_TTL_S)
    await client.pexpire(_key(token), 1)
    await asyncio.sleep(0.05)
    return token


@pytest.mark.parametrize("case", ["wrong_agent", "wrong_tenant", "expired", "chat", "recreated", "unknown"])
async def test_bad_credential_is_refused_without_legacy_fallback(case, credential_redis, legacy_calls):
    config = _config("inactive")
    ctx = _ctx()
    if case == "wrong_agent":
        token = await mint_test_credential(credential_redis, _grant(agent_slug="other"), VOICE_TTL_S)
    elif case == "wrong_tenant":
        token = await mint_test_credential(credential_redis, _grant(tenant_slug="globex"), VOICE_TTL_S)
    elif case == "expired":
        token = await _expired_token(credential_redis)
    elif case == "chat":
        token = await mint_test_credential(credential_redis, _grant(channel="chat"), CHAT_TTL_S)
    elif case == "recreated":
        token = await mint_test_credential(credential_redis, _grant(agent_id="old-agent-id"), VOICE_TTL_S)
    else:
        token = "never-minted"
    ctx.test_credential = token

    with pytest.raises(AgentUnavailable):
        await _resolve(ctx, credential_redis, config)
    assert legacy_calls == []


async def test_credential_for_a_missing_agent_is_refused(credential_redis, legacy_calls):
    token = await mint_test_credential(credential_redis, _grant(), VOICE_TTL_S)

    with pytest.raises(AgentUnavailable):
        await _resolve(_ctx(token), credential_redis, MockConfigProvider())
    assert legacy_calls == []


async def test_no_credential_inactive_and_missing_agent_reach_the_same_legacy_path(
    credential_redis, legacy_calls,
):
    inactive = await _resolve(_ctx(), credential_redis, _config("inactive"))
    missing = await _resolve(_ctx(slug="nope"), credential_redis, _config("inactive"))

    assert inactive == missing == LEGACY
    assert legacy_calls == ["sup", "nope"]


async def test_no_credential_active_agent_resolves_as_today(credential_redis, legacy_calls):
    runtime_config, _bundle = await _resolve(_ctx(), credential_redis, _config("active"))

    assert runtime_config.agent.slug == "sup"
    assert legacy_calls == []


async def test_non_test_direction_ignores_the_credential(credential_redis, legacy_calls):
    token = await mint_test_credential(credential_redis, _grant(), VOICE_TTL_S)

    result = await _resolve(_ctx(token, direction="inbound"), credential_redis, _config("inactive"))

    assert result == LEGACY
    assert legacy_calls == ["sup"]
    assert await credential_redis.exists(_key(token)) == 1


def test_session_open_request_carries_the_credential_field():
    assert pb.SessionOpenRequest(test_credential="x").test_credential == "x"
    assert pb.SessionOpenRequest().test_credential == ""


def test_repr_of_session_context_omits_the_credential():
    assert SENTINEL not in repr(_ctx(SENTINEL))
    assert _ctx(SENTINEL).test_credential == SENTINEL
