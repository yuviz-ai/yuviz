"""Multilingual agent fields: validation rules (fake connection) and tenant isolation
of per-language voice overrides (real Postgres under RLS, like test_agents.py)."""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from services.config import agents, provider_configs

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"


class FakeConn:
    """Just enough of asyncpg for _validate_languages: provider rows by id + tenant defaults
    (+ agent rows for revalidate_multilingual_agents)."""

    def __init__(self, rows: dict[str, dict], default_tts: str | None = None,
                 default_stt: str | None = None, agents: list[dict] | None = None) -> None:
        self.rows = rows
        self.default_tts = default_tts
        self.default_stt = default_stt
        self.agents = agents or []
        self.default_reads: list[str] = []

    async def fetchrow(self, sql, config_id, tenant_id=None):
        if "FROM tenants" in sql:
            self.default_reads.append(sql)
            return {"default_stt_config_id": self.default_stt, "default_tts_config_id": self.default_tts}
        row = self.rows.get(str(config_id))
        return row if row is not None and str(row["tenant_id"]) == str(tenant_id) else None

    async def fetchval(self, sql, _tenant_id):
        return self.default_stt if "default_stt_config_id" in sql else self.default_tts

    async def fetch(self, _sql, _tenant_id):
        return self.agents


def _stt(engine, model, *, name=None):
    return _tts(engine, name=name, model=model, voice=None, role="stt")


def _tts(engine, *, tenant=TENANT, name=None, model=None, voice="v", role="tts", extra=None):
    rid = str(uuid.uuid4())
    return rid, {
        "id": rid, "tenant_id": tenant, "name": name or engine, "role": role,
        "engine": engine, "model": model, "voice": voice, "extra": json.dumps(extra or {}),
    }


async def test_single_language_agent_is_not_language_checked():
    aura_id, aura = _tts("deepgram")
    out = await agents._validate_languages(
        FakeConn({aura_id: aura}), TENANT, {"language": "hi", "tts_config_id": aura_id},
    )
    assert out == {"supported_languages": None, "tts_config_by_language": None, "greeting_by_language": None}


async def test_multilingual_normalises_and_puts_default_first():
    cartesia_id, cartesia = _tts("cartesia")
    out = await agents._validate_languages(
        FakeConn({cartesia_id: cartesia}), TENANT,
        {"language": "hi-IN", "supported_languages": ["en", "HI"], "tts_config_id": cartesia_id,
         "greeting_by_language": {"hi": " नमस्ते ", "en": ""}},
    )
    assert out["language"] == "hi"
    assert out["supported_languages"] == ["hi", "en"]
    assert json.loads(out["greeting_by_language"]) == {"hi": "नमस्ते"}


async def test_english_only_base_tts_rejected_naming_language_and_provider():
    aura_id, aura = _tts("deepgram", name="Aura Asteria")
    with pytest.raises(ValueError) as exc:
        await agents._validate_languages(
            FakeConn({aura_id: aura}), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": aura_id},
        )
    assert "Hindi (hi)" in str(exc.value) and "'Aura Asteria' (deepgram)" in str(exc.value)


async def test_english_only_base_tts_accepted_with_override():
    aura_id, aura = _tts("deepgram")
    hi_id, hi = _tts("cartesia")
    out = await agents._validate_languages(
        FakeConn({aura_id: aura, hi_id: hi}), TENANT,
        {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": aura_id,
         "tts_config_by_language": {"hi": hi_id}},
    )
    assert json.loads(out["tts_config_by_language"]) == {"hi": hi_id}


async def test_tenant_default_tts_is_the_base_when_agent_has_none():
    aura_id, aura = _tts("deepgram", name="Default voice")
    with pytest.raises(ValueError, match="'Default voice'"):
        await agents._validate_languages(
            FakeConn({aura_id: aura}, default_tts=aura_id), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"]},
        )


async def test_both_tenant_defaults_come_from_one_locked_read():
    cartesia_id, cartesia = _tts("cartesia")
    whisper_id, whisper = _stt("faster_whisper", "small")
    conn = FakeConn({cartesia_id: cartesia, whisper_id: whisper}, default_tts=cartesia_id, default_stt=whisper_id)
    await agents._validate_languages(conn, TENANT, {"language": "en", "supported_languages": ["en", "hi"]}, lock_defaults=True)
    assert len(conn.default_reads) == 1
    assert "FOR SHARE" in conn.default_reads[0]
    assert "default_stt_config_id" in conn.default_reads[0] and "default_tts_config_id" in conn.default_reads[0]


async def test_english_only_elevenlabs_model_rejected():
    el_id, el = _tts("elevenlabs", extra={"model_id": "eleven_turbo_v2"})
    with pytest.raises(ValueError, match="Hindi"):
        await agents._validate_languages(
            FakeConn({el_id: el}), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": el_id},
        )


async def test_cross_tenant_override_rejected_as_not_found():
    base_id, base = _tts("cartesia")
    foreign_id, foreign = _tts("cartesia", tenant=OTHER_TENANT)
    with pytest.raises(ValueError, match=r"tts_config_by_language\['hi'\] not found"):
        await agents._validate_languages(
            FakeConn({base_id: base, foreign_id: foreign}), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": base_id,
             "tts_config_by_language": {"hi": foreign_id}},
        )


async def test_cross_tenant_override_rejected_even_for_single_language_agent():
    foreign_id, foreign = _tts("cartesia", tenant=OTHER_TENANT)
    with pytest.raises(ValueError, match="not found"):
        await agents._validate_languages(
            FakeConn({foreign_id: foreign}), TENANT, {"tts_config_by_language": {"hi": foreign_id}},
        )


async def test_override_with_wrong_role_or_language_rejected():
    base_id, base = _tts("cartesia")
    llm_id, llm = _tts("openai", role="llm")
    aura_id, aura = _tts("deepgram")
    conn = FakeConn({base_id: base, llm_id: llm, aura_id: aura})
    common = {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": base_id}
    with pytest.raises(ValueError, match="expected 'tts'"):
        await agents._validate_languages(conn, TENANT, {**common, "tts_config_by_language": {"hi": llm_id}})
    with pytest.raises(ValueError, match="voice override"):
        await agents._validate_languages(conn, TENANT, {**common, "tts_config_by_language": {"hi": aura_id}})
    with pytest.raises(ValueError, match="not in supported_languages"):
        await agents._validate_languages(conn, TENANT, {**common, "tts_config_by_language": {"es": base_id}})


async def test_unknown_language_rejected():
    with pytest.raises(ValueError, match="not supported"):
        await agents._validate_languages(FakeConn({}), TENANT, {"supported_languages": ["en", "xx"]})


# ── Real Postgres + RLS ──────────────────────────────────────────────────────

@pytest_asyncio.fixture(loop_scope="session")
async def other_tenant_tts(pool):
    """A TTS config owned by a second tenant, inserted outside the test tenant's RLS scope."""
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", f"Other {slug}", slug,
    )
    row = await pool.fetchrow(
        "INSERT INTO provider_configs (tenant_id, name, role, engine, voice) "
        "VALUES ($1, 'Foreign Hindi', 'tts', 'cartesia', 'v') RETURNING id",
        tenant["id"],
    )
    yield str(row["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def _cartesia(tenant_id, name="Cartesia"):
    return await provider_configs.create_provider_config(
        tenant_id=tenant_id, name=name, role="tts", engine="cartesia", voice="v",
        allow_pointer_schemes=False,
    )


async def test_update_rejects_other_tenants_voice_override(test_tenant, scoped, other_tenant_tts):
    base = await _cartesia(test_tenant["id"])
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="ml-agent", name="ML", tts_config_id=str(base["id"]),
        language="en", supported_languages=["en", "hi"],
    )
    with pytest.raises(ValueError, match="not found"):
        await agents.update_agent(
            created["id"], tenant_slug=test_tenant["slug"],
            tts_config_by_language={"hi": other_tenant_tts},
        )
    fetched = await agents.get_agent(test_tenant["slug"], "ml-agent")
    assert fetched["tts_config_by_language"] is None


async def test_create_and_update_round_trip_multilingual_fields(test_tenant, scoped):
    base = await _cartesia(test_tenant["id"])
    hi_voice = await _cartesia(test_tenant["id"], name="Hindi voice")
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="ml-agent", name="ML", tts_config_id=str(base["id"]),
        language="hi", supported_languages=["en"],
        tts_config_by_language={"hi": str(hi_voice["id"])},
        greeting_by_language={"hi": "नमस्ते", "en": "Hello"},
    )
    assert created["supported_languages"] == ["hi", "en"]
    assert created["tts_config_by_language"] == {"hi": str(hi_voice["id"])}
    assert created["greeting_by_language"] == {"hi": "नमस्ते", "en": "Hello"}

    updated = await agents.update_agent(created["id"], tenant_slug=test_tenant["slug"], supported_languages=[])
    assert updated["supported_languages"] is None


async def test_single_language_update_leaves_language_columns_untouched(test_tenant, scoped):
    created = await agents.create_agent(tenant_id=test_tenant["id"], slug="plain", name="Plain")
    updated = await agents.update_agent(created["id"], tenant_slug=test_tenant["slug"], language="en-US")
    assert updated["language"] == "en-US"  # single-language: stored as given, as before
    assert updated["supported_languages"] is None


async def test_provider_used_as_voice_override_cannot_be_deleted(test_tenant, scoped):
    base = await _cartesia(test_tenant["id"])
    hi_voice = await _cartesia(test_tenant["id"], name="Hindi voice")
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="ml-agent", name="ML", tts_config_id=str(base["id"]),
        language="en", supported_languages=["en", "hi"],
        tts_config_by_language={"hi": str(hi_voice["id"])},
    )
    with pytest.raises(provider_configs.ProviderConfigInUse):
        await provider_configs.soft_delete_provider_config(hi_voice["id"])


async def test_provider_used_as_voice_override_cannot_be_deleted_via_uppercase_id(test_tenant, scoped):
    base = await _cartesia(test_tenant["id"])
    hi_voice = await _cartesia(test_tenant["id"], name="Hindi voice")
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="ml-agent-upper", name="ML", tts_config_id=str(base["id"]),
        language="en", supported_languages=["en", "hi"],
        tts_config_by_language={"hi": str(hi_voice["id"])},
    )
    with pytest.raises(provider_configs.ProviderConfigInUse):
        await provider_configs.soft_delete_provider_config(str(hi_voice["id"]).upper())


async def test_kokoro_english_voice_rejected_for_hindi():
    sarah_id, sarah = _tts("kokoro", name="Kokoro Sarah", voice="af_sarah")
    with pytest.raises(ValueError, match=r"Hindi \(hi\).*'Kokoro Sarah' \(kokoro\)"):
        await agents._validate_languages(
            FakeConn({sarah_id: sarah}), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": sarah_id},
        )
    with pytest.raises(ValueError, match="voice override"):
        await agents._validate_languages(
            FakeConn({sarah_id: sarah}), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": sarah_id,
             "tts_config_by_language": {"hi": sarah_id}},
        )
    alpha_id, alpha = _tts("kokoro", voice="hf_alpha")
    out = await agents._validate_languages(
        FakeConn({sarah_id: sarah, alpha_id: alpha}), TENANT,
        {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": sarah_id,
         "tts_config_by_language": {"hi": alpha_id}},
    )
    assert json.loads(out["tts_config_by_language"]) == {"hi": alpha_id}



# ── Review fixes: STT that can't switch, language tags, provider/tenant edits ──

async def test_english_only_whisper_rejected_for_multilingual_agent():
    cartesia_id, cartesia = _tts("cartesia")
    whisper_id, whisper = _stt("faster_whisper", "small.en", name="Whisper default")
    with pytest.raises(ValueError, match=r"'Whisper default'.*English-only"):
        await agents._validate_languages(
            FakeConn({cartesia_id: cartesia, whisper_id: whisper}), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"],
             "tts_config_id": cartesia_id, "stt_config_id": whisper_id},
        )


async def test_tenant_default_english_only_whisper_also_rejected():
    cartesia_id, cartesia = _tts("cartesia")
    whisper_id, whisper = _stt("faster_whisper", "small.en")
    with pytest.raises(ValueError, match="English-only"):
        await agents._validate_languages(
            FakeConn({cartesia_id: cartesia, whisper_id: whisper}, default_stt=whisper_id), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": cartesia_id},
        )


async def test_multilingual_whisper_and_nova3_accepted_old_deepgram_rejected():
    cartesia_id, cartesia = _tts("cartesia")
    ok_whisper_id, ok_whisper = _stt("faster_whisper", "small")
    nova3_id, nova3 = _stt("deepgram", "nova-3")
    base_id, base = _stt("deepgram", "base")
    conn = FakeConn({cartesia_id: cartesia, ok_whisper_id: ok_whisper, nova3_id: nova3, base_id: base})
    common = {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": cartesia_id}
    await agents._validate_languages(conn, TENANT, {**common, "stt_config_id": ok_whisper_id})
    await agents._validate_languages(conn, TENANT, {**common, "stt_config_id": nova3_id})
    with pytest.raises(ValueError, match="nova-3 or nova-2"):
        await agents._validate_languages(conn, TENANT, {**common, "stt_config_id": base_id})


async def test_single_language_agent_keeps_any_stt():
    whisper_id, whisper = _stt("faster_whisper", "small.en")
    out = await agents._validate_languages(
        FakeConn({whisper_id: whisper}), TENANT, {"language": "en-US", "stt_config_id": whisper_id},
    )
    assert out["supported_languages"] is None


@pytest.mark.parametrize("value", ["en", "hi", "en-US", "nl-BE", "zh-Hant-TW", None])
def test_language_tag_shape_accepted(value):
    agents._check_language_tag(value)


@pytest.mark.parametrize("value", ["", "english", "en US", "1234", "en-", "en_US", 5])
def test_language_tag_shape_rejected(value):
    with pytest.raises(ValueError, match="not a language code"):
        agents._check_language_tag(value)


async def test_revalidation_names_the_agent_a_provider_edit_would_break():
    # The Hindi override's voice was just edited to an English Kokoro voice.
    base_id, base = _tts("cartesia")
    hi_id, hi = _tts("kokoro", voice="af_heart", name="Hindi voice")
    agent_row = {
        "name": "Clinic bot", "language": "en", "supported_languages": ["en", "hi"],
        "tts_config_by_language": json.dumps({"hi": hi_id}), "greeting_by_language": None,
        "tts_config_id": base_id, "stt_config_id": None,
    }
    conn = FakeConn({base_id: base, hi_id: hi}, agents=[agent_row])
    with pytest.raises(ValueError, match=r"'Clinic bot'.*Hindi"):
        await agents.revalidate_multilingual_agents(conn, TENANT, provider_id=hi_id)
    # An edit to a row this agent doesn't use is never blocked by it.
    await agents.revalidate_multilingual_agents(conn, TENANT, provider_id=str(uuid.uuid4()))


async def test_revalidation_covers_agents_falling_back_to_the_tenant_default():
    aura_id, aura = _tts("deepgram", name="New default voice")
    agent_row = {
        "name": "Default-voice bot", "language": "en", "supported_languages": ["en", "hi"],
        "tts_config_by_language": None, "greeting_by_language": None,
        "tts_config_id": None, "stt_config_id": None,
    }
    conn = FakeConn({aura_id: aura}, default_tts=aura_id, agents=[agent_row])
    with pytest.raises(ValueError, match="'Default-voice bot'"):
        await agents.revalidate_multilingual_agents(conn, TENANT, default_roles=("tts",))
    with pytest.raises(ValueError, match="'Default-voice bot'"):
        await agents.revalidate_multilingual_agents(conn, TENANT, provider_id=aura_id)  # edited the default row
    await agents.revalidate_multilingual_agents(conn, TENANT, default_roles=())


async def test_revalidation_passes_when_agents_still_work():
    base_id, base = _tts("cartesia")
    agent_row = {
        "name": "Clinic bot", "language": "en", "supported_languages": ["en", "hi"],
        "tts_config_by_language": None, "greeting_by_language": None,
        "tts_config_id": base_id, "stt_config_id": None,
    }
    await agents.revalidate_multilingual_agents(FakeConn({base_id: base}, agents=[agent_row]), TENANT, provider_id=base_id)


async def test_provider_edit_that_breaks_a_multilingual_agent_is_refused(test_tenant, scoped):
    base = await _cartesia(test_tenant["id"])
    hi_voice = await provider_configs.create_provider_config(
        tenant_id=test_tenant["id"], name="Hindi voice", role="tts", engine="kokoro", voice="hf_alpha",
        allow_pointer_schemes=False,
    )
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="ml-agent", name="ML", tts_config_id=str(base["id"]),
        language="en", supported_languages=["en", "hi"],
        tts_config_by_language={"hi": str(hi_voice["id"])},
    )
    with pytest.raises(ValueError, match="would break multilingual agent 'ML'"):
        await provider_configs.update_provider_config(hi_voice["id"], allow_pointer_schemes=False, voice="af_heart")
    still = await provider_configs.get_provider_config(hi_voice["id"])
    assert still["voice"] == "hf_alpha"


async def test_agent_save_rejects_malformed_language(test_tenant, scoped):
    created = await agents.create_agent(tenant_id=test_tenant["id"], slug="plain2", name="Plain")
    with pytest.raises(ValueError, match="not a language code"):
        await agents.update_agent(created["id"], tenant_slug=test_tenant["slug"], language="english please")



async def test_language_is_stored_trimmed(test_tenant, scoped):
    created = await agents.create_agent(tenant_id=test_tenant["id"], slug="trim", name="Trim", language=" en-US ")
    assert created["language"] == "en-US"
    updated = await agents.update_agent(created["id"], tenant_slug=test_tenant["slug"], language=" hi ")
    assert updated["language"] == "hi"



async def test_revalidation_matches_ids_regardless_of_case():
    base_id, base = _tts("cartesia")
    hi_id, hi = _tts("kokoro", voice="af_heart")
    agent_row = {
        "name": "Clinic bot", "language": "en", "supported_languages": ["en", "hi"],
        "tts_config_by_language": json.dumps({"hi": hi_id.upper()}), "greeting_by_language": None,
        "tts_config_id": base_id, "stt_config_id": None,
    }
    conn = FakeConn({base_id: base, hi_id: hi, hi_id.upper(): hi}, agents=[agent_row])
    with pytest.raises(ValueError, match="'Clinic bot'"):
        await agents.revalidate_multilingual_agents(conn, TENANT, provider_id=hi_id.upper())
    with pytest.raises(ValueError, match="'Clinic bot'"):
        await agents.revalidate_multilingual_agents(conn, TENANT, provider_id=hi_id)


async def test_override_ids_are_stored_lowercase():
    base_id, base = _tts("cartesia")
    hi_id, hi = _tts("cartesia")
    out = await agents._validate_languages(
        FakeConn({base_id: base, hi_id: hi, hi_id.upper(): hi}), TENANT,
        {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": base_id,
         "tts_config_by_language": {"hi": hi_id.upper()}},
    )
    assert json.loads(out["tts_config_by_language"]) == {"hi": hi_id}


async def test_create_rejects_other_tenants_voice_override(test_tenant, scoped, other_tenant_tts):
    # The wizard now sends the language fields in the create request itself.
    base = await _cartesia(test_tenant["id"])
    with pytest.raises(ValueError, match="not found"):
        await agents.create_agent(
            tenant_id=test_tenant["id"], slug="ml-create", name="ML", tts_config_id=str(base["id"]),
            language="en", supported_languages=["en", "hi"],
            tts_config_by_language={"hi": other_tenant_tts},
        )
    assert await agents.get_agent(test_tenant["slug"], "ml-create") is None



async def test_voiceless_kokoro_base_is_rejected_for_hindi():
    # The factory voices it with the default English voice, so it can't be the Hindi voice.
    kokoro_id, kokoro = _tts("kokoro", name="Kokoro (no voice)", voice=None)
    with pytest.raises(ValueError, match=r"Hindi \(hi\).*'Kokoro \(no voice\)'"):
        await agents._validate_languages(
            FakeConn({kokoro_id: kokoro}), TENANT,
            {"language": "en", "supported_languages": ["en", "hi"], "tts_config_id": kokoro_id},
        )


async def test_agent_save_reads_tenant_defaults_for_share():
    conn = FakeConn({})
    await agents._validate_languages(
        conn, TENANT, {"language": "en", "supported_languages": ["en"]}, lock_defaults=True,
    )
    assert len(conn.default_reads) == 1 and "FOR SHARE" in conn.default_reads[0]


async def test_revalidation_reads_tenant_defaults_without_a_lock():
    # Taking the tenant lock after the provider lock would invert update_tenant's order.
    cartesia_id, cartesia = _tts("cartesia")
    conn = FakeConn({cartesia_id: cartesia}, default_tts=cartesia_id, agents=[{
        "name": "Bot", "language": "en", "supported_languages": ["en", "hi"],
        "tts_config_by_language": None, "greeting_by_language": None,
        "tts_config_id": None, "stt_config_id": None,
    }])
    await agents.revalidate_multilingual_agents(conn, TENANT, default_roles=("tts",))
    assert len(conn.default_reads) == 1 and "FOR SHARE" not in conn.default_reads[0]
