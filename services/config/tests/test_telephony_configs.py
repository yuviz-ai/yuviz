"""T20: provider-agnostic _normalize_credentials/validate_credentials over
sensitive_credential_fields() (scalar and list), the native-provider
early-return, list_supported_providers()'s {required, sensitive} shape,
and list_telephony_configs()'s per-row health dict."""

from __future__ import annotations

import json

import pytest

from libs.config_sdk.secrets import generate_key
from services.config import cache, telephony_configs


@pytest.fixture(autouse=True)
def _secret_encryption_key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


class TestNormalizeCredentials:
    async def test_vobiz_scalar_auth_token_seals_to_enc(self):
        result = telephony_configs._normalize_credentials(
            "vobiz", {"auth_id": "aid", "auth_token": "plaintext-token"},
        )
        assert result["auth_token"].startswith("enc:")
        assert result["auth_token"] != "plaintext-token"

    async def test_cloudonix_list_api_keys_seals_every_entry(self):
        result = telephony_configs._normalize_credentials(
            "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["key-1", "key-2"]},
        )
        assert all(k.startswith("enc:") for k in result["api_keys"])
        assert result["api_keys"] != ["key-1", "key-2"]

    async def test_already_enc_value_round_trips_un_double_encrypted(self):
        once = telephony_configs._normalize_credentials(
            "vobiz", {"auth_id": "aid", "auth_token": "plaintext-token"},
        )
        twice = telephony_configs._normalize_credentials("vobiz", once)
        assert once["auth_token"] == twice["auth_token"]

    async def test_env_ref_rejected_for_scalar_field(self):
        with pytest.raises(ValueError):
            telephony_configs._normalize_credentials(
                "vobiz", {"auth_id": "aid", "auth_token": "env:SOME_VAR"},
            )

    async def test_k8s_ref_rejected_for_list_field(self):
        with pytest.raises(ValueError):
            telephony_configs._normalize_credentials(
                "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["k8s:/etc/passwd"]},
            )

    async def test_native_provider_returns_credentials_verbatim(self):
        credentials = {"anything": "env:whatever-not-checked"}
        result = telephony_configs._normalize_credentials("native", credentials)
        assert result is credentials or result == credentials

    async def test_native_provider_skips_validate(self):
        telephony_configs.validate_credentials("native", {"anything": "goes"})  # must not raise


class TestListSupportedProviders:
    def test_excludes_fake_and_exposes_required_and_sensitive(self):
        supported = telephony_configs.list_supported_providers()
        assert "fake" not in supported
        assert set(supported["vobiz"].keys()) == {"required", "sensitive"}
        assert supported["vobiz"]["sensitive"] == ["auth_token"]
        assert supported["cloudonix"]["sensitive"] == ["api_keys"]


class TestListTelephonyConfigsHealth:
    async def test_row_gets_health_from_redis_or_standby_default(self, test_tenant, scoped, pool):
        row = await pool.fetchrow(
            "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
            "VALUES ($1, 'Vobiz', 'vobiz', $2::jsonb) RETURNING id",
            test_tenant["id"], json.dumps({"auth_id": "aid", "auth_token": "enc:x"}),
        )
        config_id = row["id"]
        try:
            configs = await telephony_configs.list_telephony_configs(test_tenant["id"])
            found = next(c for c in configs if c["id"] == config_id)
            assert found["health"] == {"status": "standby", "checked_at": None}

            await cache.set_json(
                f"telephony:health:{config_id}", {"status": "healthy", "checked_at": "2026-01-01T00:00:00+00:00"},
            )
            configs = await telephony_configs.list_telephony_configs(test_tenant["id"])
            found = next(c for c in configs if c["id"] == config_id)
            assert found["health"]["status"] == "healthy"
        finally:
            await cache.invalidate(f"telephony:health:{config_id}")
            await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config_id)
