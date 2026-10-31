"""Platform-managed AI: catalogue, recommended picks, workspace rows and their lock."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import generate_key
from services.config import auth, platform_ai, provider_configs
from services.config import users as users_service
from services.config.app import app

_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "DEEPGRAM_API_KEY",
         "ELEVENLABS_API_KEY", "CARTESIA_API_KEY", "SARVAM_API_KEY")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-not-real")


@pytest_asyncio.fixture
async def local_off():
    before = (await platform_ai.get_settings())["local_models_enabled"]
    await platform_ai.set_settings(local_models_enabled=False, user_id=None)
    yield
    await platform_ai.set_settings(local_models_enabled=before, user_id=None)


def test_catalog_hides_local_models_until_enabled():
    assert not [o for o in platform_ai.catalog(False) if o["local"]]
    assert [o for o in platform_ai.catalog(True) if o["engine"] == "ollama"]


def test_catalog_marks_cloud_options_without_a_platform_key_unavailable():
    by_id = {o["id"]: o for o in platform_ai.catalog(False)}
    assert by_id["llm:openai:gpt-4o-mini"]["available"] is True
    assert by_id["llm:anthropic:claude-haiku-4-5"]["available"] is False
    assert "key_env" not in by_id["llm:openai:gpt-4o-mini"]


def test_recommended_prefers_sarvam_for_hindi(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "s-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "e-test")
    option, voice = platform_ai.recommended("tts", ["hi", "en"], False)
    assert option.engine == "sarvam" and voice
    option, _ = platform_ai.recommended("tts", ["en"], False)
    assert option.engine == "elevenlabs"


def test_recommended_hearing_must_switch_between_the_languages():
    option, _ = platform_ai.recommended("stt", ["en", "hi"], False)
    assert option.id == "stt:deepgram:nova-3"
    # Deepgram's multi mode doesn't cover Chinese; without local models nothing fits.
    assert platform_ai.recommended("stt", ["en", "zh"], False) is None
    option, _ = platform_ai.recommended("stt", ["en", "zh"], True)
    assert option.engine == "faster_whisper" and not option.model.endswith(".en")


def test_recommended_is_none_when_no_platform_key_is_set(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    assert platform_ai.recommended("llm", ["en"], False) is None


def test_managed_rows_accept_only_speed_and_think():
    old = {"extra": {"managed": "tts:sarvam:bulbul:v4-flash", "speed": 1.0}}
    provider_configs.check_managed_edit(old, {"extra": "{}"}, {**old["extra"], "speed": 1.1}, had_api_key=False)
    for fields, extra, typed in (
        ({"model": "x"}, None, False),
        ({"extra": "{}"}, {"speed": 1.0}, False),
        ({"extra": "{}"}, {**old["extra"], "model": "other"}, False),
        ({"api_key_ref": "env:OTHER"}, None, False),
        ({}, None, True),
    ):
        with pytest.raises(ValueError, match="provided by the platform"):
            provider_configs.check_managed_edit(old, fields, extra, had_api_key=typed)


def test_a_workspace_row_cannot_claim_to_be_managed():
    with pytest.raises(ValueError, match="reserved"):
        provider_configs.check_managed_edit({"extra": {}}, {"extra": "{}"}, {"managed": "llm:openai:gpt-4o"}, had_api_key=False)


async def test_choose_reuses_one_row_per_option_and_setting(scoped, test_tenant, local_off):
    tid = test_tenant["id"]
    first = await platform_ai.choose(tid, "llm:openai:gpt-4o-mini")
    assert first["api_key_ref"] == "env:OPENAI_API_KEY"
    assert first["extra"]["managed"] == "llm:openai:gpt-4o-mini"
    assert (await platform_ai.choose(tid, "llm:openai:gpt-4o-mini"))["id"] == first["id"]
    warmer = await platform_ai.choose(tid, "llm:openai:gpt-4o-mini", temperature=0.9)
    assert warmer["id"] != first["id"] and warmer["extra"]["temperature"] == 0.9


async def test_choose_refuses_unavailable_and_hidden_options(scoped, test_tenant, local_off):
    tid = test_tenant["id"]
    with pytest.raises(ValueError, match="not available"):
        await platform_ai.choose(tid, "llm:anthropic:claude-haiku-4-5")
    with pytest.raises(ValueError, match="not available"):
        await platform_ai.choose(tid, "llm:ollama:llama3.2")
    with pytest.raises(ValueError, match="unknown"):
        await platform_ai.choose(tid, "llm:openai:../../etc")


async def test_workspace_edits_of_a_managed_row_are_refused(scoped, test_tenant, local_off):
    row = await platform_ai.choose(test_tenant["id"], "stt:deepgram:nova-3")
    with pytest.raises(ValueError, match="provided by the platform"):
        await provider_configs.update_provider_config(row["id"], allow_pointer_schemes=False, model="nova-2")
    with pytest.raises(ValueError, match="provided by the platform"):
        await provider_configs.update_provider_config(row["id"], allow_pointer_schemes=False, api_key="sk-mine")
    with pytest.raises(ValueError, match="reserved"):
        await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="fake", role="llm", engine="openai", api_key="sk-mine",
            extra={"managed": "llm:openai:gpt-4o"}, allow_pointer_schemes=False,
        )


async def _client(pool, test_tenant, role: str, *, tenant: bool) -> AsyncClient:
    user = await users_service.create_user(
        email=f"test-{role}-{uuid.uuid4().hex[:8]}@example.com", password="test-password-not-real", role=role,
        tenant_id=test_tenant["id"] if tenant else None,
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                         headers={"Authorization": f"Bearer {auth.create_access_token(user)}"})
    client.user_id = user["id"]
    return client


async def test_only_a_superadmin_changes_the_local_models_switch(pool, test_tenant, local_off):
    admin = await _client(pool, test_tenant, "admin", tenant=True)
    root = await _client(pool, test_tenant, "superadmin", tenant=False)
    try:
        assert (await admin.put("/platform/ai-settings", json={"local_models_enabled": True})).status_code == 403
        assert (await admin.get("/platform/ai-settings")).status_code == 403
        keys = {k["key_env"]: k for k in (await root.get("/platform/ai-settings")).json()["keys"]}
        assert keys["OPENAI_API_KEY"]["configured"] is True and keys["ANTHROPIC_API_KEY"]["configured"] is False
        assert "sk-test-not-real" not in str(keys)
        resp = await root.put("/platform/ai-settings", json={"local_models_enabled": True})
        assert resp.status_code == 200 and resp.json()["local_models_enabled"] is True
        listed = (await admin.get(f"/tenants/{test_tenant['id']}/ai-options")).json()
        assert listed["local_models_enabled"] is True
        assert any(o["local"] for o in listed["options"])
        assert not any("key_env" in o for o in listed["options"])
    finally:
        for c in (admin, root):
            await c.aclose()
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", c.user_id)


async def test_recommended_route_returns_masked_workspace_rows(pool, test_tenant, local_off):
    admin = await _client(pool, test_tenant, "admin", tenant=True)
    try:
        resp = await admin.post(f"/tenants/{test_tenant['id']}/ai-options/recommended", json={"languages": ["en"]})
        assert resp.status_code == 200
        body = {r["role"]: r for r in resp.json()}
        assert body["llm"]["api_key_ref"] == "[stored]" and body["stt"]["engine"] == "deepgram"
        assert body["tts"]["engine"] == "deepgram" and body["tts"]["voice"] == "aura-asteria-en"
        bad = await admin.post(f"/tenants/{test_tenant['id']}/ai-options/recommended", json={"languages": ["xx"]})
        assert bad.status_code == 422
    finally:
        await admin.aclose()
        await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", admin.user_id)


async def test_preview_refuses_local_and_unconfigured_voices():
    with pytest.raises(ValueError, match="cloud voices"):
        await platform_ai.option_preview("tts:kokoro:kokoro", "af_sarah", "en", secret_resolver=None)
    with pytest.raises(ValueError, match="not available"):
        await platform_ai.option_preview("tts:elevenlabs:eleven_flash_v2_5", "EXAVITQu4vr4xnSDxMaL", "en", secret_resolver=None)


async def test_sarvam_speakers_must_belong_to_the_chosen_model(scoped, test_tenant, local_off, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "s-test")
    tid = test_tenant["id"]
    assert (await platform_ai.choose(tid, "tts:sarvam:bulbul:v3", voice="kavya"))["model"] == "bulbul:v3"
    with pytest.raises(ValueError, match="Bulbul v3 speaker"):
        await platform_ai.choose(tid, "tts:sarvam:bulbul:v3", voice="shubh_enhi_ads")
    with pytest.raises(ValueError, match="v4 Flash persona"):
        await platform_ai.choose(tid, "tts:sarvam:bulbul:v4-flash", voice="kavya")
