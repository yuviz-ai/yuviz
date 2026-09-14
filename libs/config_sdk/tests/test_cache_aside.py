"""
Fake IConfigRepository implementations, not real Redis/HTTP — this file is
about CacheAsideConfigProvider's own orchestration logic (fallback order,
dict-to-DTO mapping, agent-override-vs-tenant-default, all-or-nothing
resolution), which doesn't need real infra to prove. Real-infra coverage
lives in test_redis_repository.py/test_http_repository.py.
"""

from __future__ import annotations

from datetime import datetime, timezone


from libs.config_sdk.providers.cache_aside import CacheAsideConfigProvider


def _now():
    return datetime.now(timezone.utc).isoformat()


class FakeRepo:
    """Implements IConfigRepository against an in-memory dict store —
    counts calls so tests can assert the HTTP fallback was (or wasn't)
    reached."""

    def __init__(self, tenants=None, agents=None, providers=None):
        self.tenants = tenants or {}
        self.agents = agents or {}
        self.providers = providers or {}
        self.calls: list[str] = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def fetch_tenant(self, tenant_slug):
        self.calls.append(f"tenant:{tenant_slug}")
        return self.tenants.get(tenant_slug)

    async def fetch_agent(self, tenant_slug, agent_slug):
        self.calls.append(f"agent:{tenant_slug}:{agent_slug}")
        return self.agents.get((tenant_slug, agent_slug))

    async def fetch_provider_config(self, provider_id):
        self.calls.append(f"provider:{provider_id}")
        return self.providers.get(provider_id)


def _tenant_row(slug, **overrides):
    return {
        "id": "t1", "slug": slug, "name": "Test", "region": "us",
        "config_version": 1, "updated_at": _now(),
        "default_stt_config_id": None, "default_llm_config_id": None, "default_tts_config_id": None,
        **overrides,
    }


def _agent_row(slug, **overrides):
    return {
        "id": "a1", "slug": slug, "tenant_id": "t1", "name": "Agent",
        "greeting": "Hi", "system_prompt": "Be helpful.", "goodbye_grace_ms": 3000,
        "status": "active", "config_version": 1, "updated_at": _now(),
        "stt_config_id": None, "llm_config_id": None, "tts_config_id": None,
        **overrides,
    }


def _provider_row(provider_id, role, engine, **overrides):
    return {"id": provider_id, "role": role, "engine": engine, "extra": "{}", **overrides}


async def test_get_tenant_hits_redis_and_skips_http():
    redis_repo = FakeRepo(tenants={"acme": _tenant_row("acme")})
    http_repo = FakeRepo()
    provider = CacheAsideConfigProvider(redis_repo, http_repo)

    tenant = await provider.get_tenant("acme")
    assert tenant is not None and tenant.slug == "acme"
    assert http_repo.calls == []  # never reached


async def test_get_tenant_falls_through_to_http_on_redis_miss():
    redis_repo = FakeRepo()
    http_repo = FakeRepo(tenants={"acme": _tenant_row("acme")})
    provider = CacheAsideConfigProvider(redis_repo, http_repo)

    tenant = await provider.get_tenant("acme")
    assert tenant is not None and tenant.slug == "acme"
    assert http_repo.calls == ["tenant:acme"]


async def test_get_tenant_both_miss_returns_none():
    provider = CacheAsideConfigProvider(FakeRepo(), FakeRepo())
    assert await provider.get_tenant("no-such-tenant") is None


async def test_provider_config_extra_jsonb_string_is_parsed():
    redis_repo = FakeRepo(providers={"p1": _provider_row("p1", "stt", "deepgram", extra='{"device":"cpu"}')})
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    cfg = await provider.get_provider_config("p1")
    assert cfg.extra == {"device": "cpu"}


async def test_runtime_config_no_agent_returns_none():
    provider = CacheAsideConfigProvider(FakeRepo(), FakeRepo())
    assert await provider.get_runtime_config("acme", "no-such-agent") is None


async def test_runtime_config_inactive_agent_returns_none():
    redis_repo = FakeRepo(agents={("acme", "sup"): _agent_row("sup", status="inactive")})
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())
    assert await provider.get_runtime_config("acme", "sup") is None


async def test_runtime_config_uses_tenant_defaults():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup", greeting="Hi there", system_prompt="Be helpful.")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc is not None
    assert rc.providers.stt.engine == "deepgram"
    assert rc.providers.llm.engine == "openai"
    assert rc.providers.tts.engine == "elevenlabs"
    assert rc.conversation.greeting == "Hi there"
    assert rc.conversation.system_prompt == "Be helpful."
    assert rc.conversation.workflow is None
    assert rc.conversation.workflow_draft is None
    assert rc.version == 1


async def test_runtime_config_agent_override_takes_precedence_over_tenant_default():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="tenant-stt", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup", stt_config_id="agent-stt")},
        providers={
            "tenant-stt": _provider_row("tenant-stt", "stt", "deepgram", model="tenant-model"),
            "agent-stt": _provider_row("agent-stt", "stt", "deepgram", model="agent-model"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.providers.stt.model == "agent-model"


async def test_runtime_config_missing_one_provider_role_returns_none():
    # Tenant has stt+llm defaults but no tts default, agent has no override
    # either — incomplete config is unavailable, not partially applied.
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row("acme", default_stt_config_id="stt1", default_llm_config_id="llm1")},
        agents={("acme", "sup"): _agent_row("sup")},
        providers={"stt1": _provider_row("stt1", "stt", "deepgram"), "llm1": _provider_row("llm1", "llm", "openai")},
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())
    assert await provider.get_runtime_config("acme", "sup") is None


async def test_get_prompt_returns_agent_greeting_and_prompt():
    redis_repo = FakeRepo(agents={("acme", "sup"): _agent_row("sup", greeting="Hi", system_prompt="Help.")})
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    prompt = await provider.get_prompt("acme", "sup")
    assert prompt.greeting == "Hi"
    assert prompt.system_prompt == "Help."


async def test_get_voice_derives_from_runtime_config():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs", voice="Rachel"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())
    assert await provider.get_voice("acme", "sup") == "Rachel"


async def test_get_tools_returns_empty_list_not_error():
    provider = CacheAsideConfigProvider(FakeRepo(), FakeRepo())
    assert await provider.get_tools("acme", "sup") == []


async def test_runtime_config_media_flattens_voice_and_language_from_providers():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram", language="en"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs", voice="Rachel"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.media.voice == "Rachel"
    assert rc.media.language == "en"  # from STT, not TTS — see MediaInfo's docstring


async def test_runtime_config_media_language_agent_override_wins_over_provider():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup", language="fr")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram", language="en"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.media.language == "fr"  # explicit agent override beats the STT provider's "en"


async def test_runtime_config_policies_sourced_from_tenant_and_agent():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
            vad_engine="silero", vad_hold_ms=500, no_speech_timeout_ms=8000,
        )},
        agents={("acme", "sup"): _agent_row("sup", goodbye_grace_ms=4000)},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.policies.vad_engine == "silero"
    assert rc.policies.vad_hold_ms == 500
    assert rc.policies.silence_timeout_ms == 8000
    assert rc.policies.goodbye_grace_ms == 4000
    # No schema column exists for this yet — honestly None, not invented.
    assert rc.policies.barge_in_enabled is None
    # max_call_duration_s does have a real column; unset on the row still
    # means "unlimited" (None), not a magically invented default.
    assert rc.policies.max_call_duration_s is None


async def test_runtime_config_max_call_duration_sourced_from_agent():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup", max_call_duration_s=600)},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.policies.max_call_duration_s == 600


async def test_agent_transfer_fields_default_when_absent_from_row():
    # Backward compatibility: a row cached before these columns existed (or
    # a raw dict missing them for any other reason) must still deserialize,
    # matching the column defaults (transfer_type='none', rest nullable).
    redis_repo = FakeRepo(agents={("acme", "sup"): _agent_row("sup")})
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    agent = await provider.get_agent("acme", "sup")
    assert agent.transfer_type == "none"
    assert agent.transfer_destination is None
    assert agent.queue_id is None
    assert agent.escalation_threshold is None


async def test_agent_transfer_fields_flow_through_from_row():
    redis_repo = FakeRepo(agents={("acme", "sup"): _agent_row(
        "sup", transfer_type="warm", transfer_destination="+15551234567",
        queue_id="support-tier-1", escalation_threshold=3,
    )})
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    agent = await provider.get_agent("acme", "sup")
    assert agent.transfer_type == "warm"
    assert agent.transfer_destination == "+15551234567"
    assert agent.queue_id == "support-tier-1"
    assert agent.escalation_threshold == 3


async def test_runtime_config_policies_carries_transfer_fields():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row(
            "sup", transfer_type="cold", transfer_destination="+15559876543",
            queue_id="billing", escalation_threshold=2,
        )},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.policies.transfer_type == "cold"
    assert rc.policies.transfer_destination == "+15559876543"
    assert rc.policies.queue_id == "billing"
    assert rc.policies.escalation_threshold == 2


async def test_runtime_config_policies_transfer_defaults_when_agent_row_lacks_columns():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.policies.transfer_type == "none"
    assert rc.policies.transfer_destination is None
    assert rc.policies.queue_id is None
    assert rc.policies.escalation_threshold is None


async def test_close_delegates_to_both_repositories():
    redis_repo, http_repo = FakeRepo(), FakeRepo()
    provider = CacheAsideConfigProvider(redis_repo, http_repo)
    await provider.close()
    assert redis_repo.closed and http_repo.closed


async def test_runtime_config_transfer_timeout_sourced_from_tenant():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
            transfer_timeout_ms=60_000,
        )},
        agents={("acme", "sup"): _agent_row("sup")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.policies.transfer_timeout_ms == 60_000


async def test_runtime_config_transfer_timeout_defaults_when_tenant_row_lacks_column():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
        )},
        agents={("acme", "sup"): _agent_row("sup")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.policies.transfer_timeout_ms == 45_000  # TRANSFER_TIMEOUT_DEFAULT_MS


async def test_runtime_config_transfer_timeout_out_of_bounds_falls_back_to_default():
    redis_repo = FakeRepo(
        tenants={"acme": _tenant_row(
            "acme", default_stt_config_id="stt1", default_llm_config_id="llm1", default_tts_config_id="tts1",
            transfer_timeout_ms=5,  # below the 10s floor
        )},
        agents={("acme", "sup"): _agent_row("sup")},
        providers={
            "stt1": _provider_row("stt1", "stt", "deepgram"),
            "llm1": _provider_row("llm1", "llm", "openai"),
            "tts1": _provider_row("tts1", "tts", "elevenlabs"),
        },
    )
    provider = CacheAsideConfigProvider(redis_repo, FakeRepo())

    rc = await provider.get_runtime_config("acme", "sup")
    assert rc.policies.transfer_timeout_ms == 45_000
