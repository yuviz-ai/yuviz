"""Telephony credential sealing/validation, supported-provider shape, and per-row health."""

from __future__ import annotations

import inspect
import json

import pytest

from libs.config_sdk.secrets import decrypt_secret, generate_key
from services.config import cache, telephony_configs
from services.config.provider_configs import STORED_SENTINEL


@pytest.fixture(autouse=True)
def _secret_encryption_key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


class TestNormalizeCredentials:
    async def test_vobiz_scalar_auth_token_seals_to_enc(self):
        result = telephony_configs._normalize_credentials(
            "vobiz", {"auth_id": "aid", "auth_token": "plaintext-token"},
            allow_pointer_schemes=False,
        )
        assert result["auth_token"].startswith("enc:")
        assert result["auth_token"] != "plaintext-token"

    async def test_cloudonix_list_api_keys_seals_every_entry(self):
        result = telephony_configs._normalize_credentials(
            "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["key-1", "key-2"]},
            allow_pointer_schemes=False,
        )
        assert all(k.startswith("enc:") for k in result["api_keys"])
        assert result["api_keys"] != ["key-1", "key-2"]

    async def test_already_enc_value_round_trips_un_double_encrypted(self):
        once = telephony_configs._normalize_credentials(
            "vobiz", {"auth_id": "aid", "auth_token": "plaintext-token"},
            allow_pointer_schemes=False,
        )
        twice = telephony_configs._normalize_credentials(
            "vobiz", once, once, allow_pointer_schemes=False,
        )
        assert once["auth_token"] == twice["auth_token"]

    async def test_another_tenants_enc_value_is_refused_and_does_not_leak_it(self):
        mine = telephony_configs._normalize_credentials(
            "vobiz", {"auth_id": "aid", "auth_token": "mine"}, allow_pointer_schemes=False,
        )
        theirs = telephony_configs._normalize_credentials(
            "vobiz", {"auth_id": "aid", "auth_token": "theirs"}, allow_pointer_schemes=False,
        )
        for old in (None, mine):
            with pytest.raises(ValueError, match="auth_token: credential_ref_not_accepted") as exc:
                telephony_configs._normalize_credentials(
                    "vobiz", theirs, old, allow_pointer_schemes=False,
                )
            assert theirs["auth_token"] not in str(exc.value)

    async def test_another_tenants_list_entry_is_refused(self):
        mine = telephony_configs._normalize_credentials(
            "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["m1", "m2"]}, allow_pointer_schemes=False,
        )
        theirs = telephony_configs._normalize_credentials(
            "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["t1"]}, allow_pointer_schemes=False,
        )
        with pytest.raises(ValueError, match="credential_ref_not_accepted"):
            telephony_configs._normalize_credentials(
                "cloudonix", {"domain": "a.cloudonix.io", "api_keys": theirs["api_keys"]}, mine,
                allow_pointer_schemes=False,
            )

    async def test_stored_sentinel_maps_to_the_old_entry_by_index(self):
        mine = telephony_configs._normalize_credentials(
            "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["m1", "m2"]}, allow_pointer_schemes=False,
        )
        result = telephony_configs._normalize_credentials(
            "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["[stored]", "[stored]"]}, mine,
            allow_pointer_schemes=False,
        )
        assert result["api_keys"] == mine["api_keys"]

    async def test_stored_sentinel_without_an_old_entry_is_refused(self):
        with pytest.raises(ValueError, match="credential_ref_not_accepted"):
            telephony_configs._normalize_credentials(
                "vobiz", {"auth_id": "aid", "auth_token": "[stored]"}, allow_pointer_schemes=True,
            )
        with pytest.raises(ValueError, match="credential_ref_not_accepted"):
            telephony_configs._normalize_credentials(
                "cloudonix", {"domain": "d", "api_keys": ["[stored]", "[stored]"]},
                {"api_keys": ["enc:only-one"]}, allow_pointer_schemes=True,
            )

    async def test_pointer_schemes_pass_only_when_allowed(self):
        creds = {"auth_id": "aid", "auth_token": "env:SOME_VAR"}
        assert telephony_configs._normalize_credentials(
            "vobiz", creds, allow_pointer_schemes=True,
        )["auth_token"] == "env:SOME_VAR"
        with pytest.raises(ValueError, match="credential_ref_not_accepted"):
            telephony_configs._normalize_credentials("vobiz", creds, allow_pointer_schemes=False)

    async def test_allow_pointer_schemes_is_required(self):
        # Inspected, not called: a call without the keyword would trip the
        # call-site tripwire in test_credential_masking.py.
        for fn in (telephony_configs._normalize_credentials, telephony_configs._normalize_one):
            param = inspect.signature(fn).parameters["allow_pointer_schemes"]
            assert param.default is inspect.Parameter.empty
            assert param.kind is inspect.Parameter.KEYWORD_ONLY

    async def test_env_ref_rejected_for_scalar_field(self):
        with pytest.raises(ValueError):
            telephony_configs._normalize_credentials(
                "vobiz", {"auth_id": "aid", "auth_token": "env:SOME_VAR"},
                allow_pointer_schemes=False,
            )

    async def test_k8s_ref_rejected_for_list_field(self):
        with pytest.raises(ValueError):
            telephony_configs._normalize_credentials(
                "cloudonix", {"domain": "a.cloudonix.io", "api_keys": ["k8s:/etc/passwd"]},
                allow_pointer_schemes=False,
            )

    async def test_native_provider_returns_credentials_verbatim(self):
        credentials = {"anything": "env:whatever-not-checked"}
        result = telephony_configs._normalize_credentials("native", credentials, allow_pointer_schemes=False)
        assert result is credentials or result == credentials

    async def test_public_telephony_config_masks_every_enc_string_including_native(self):
        cfg = {"credentials": {
            "auth_token": "enc:aaa", "api_keys": ["enc:b", "env:X"], "auth_id": "aid", "n": 3,
        }}
        masked = telephony_configs.public_telephony_config(cfg, masked=True)["credentials"]
        assert masked == {"auth_token": "[stored]", "api_keys": ["[stored]", "env:X"], "auth_id": "aid", "n": 3}
        assert cfg["credentials"]["auth_token"] == "enc:aaa"  # input is not mutated
        assert telephony_configs.public_telephony_config(cfg, masked=False) is cfg
        native = {"credentials": {"anything": "enc:zzz"}}
        assert telephony_configs.public_telephony_config(native, masked=True)["credentials"] == {"anything": "[stored]"}

    async def test_native_provider_skips_validate(self):
        telephony_configs.validate_credentials("native", {"anything": "goes"})  # must not raise


class TestListSupportedProviders:
    def test_excludes_fake_and_exposes_required_and_sensitive(self):
        supported = telephony_configs.list_supported_providers()
        assert "fake" not in supported
        assert set(supported["vobiz"].keys()) == {"required", "sensitive"}
        assert supported["vobiz"]["sensitive"] == ["auth_token"]
        assert supported["cloudonix"]["sensitive"] == ["api_keys", "account_api_key"]


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


def test_plaintext_sensitive_credential_is_masked_like_a_sealed_one():
    # Rows saved before sealing existed (2026-09-25) hold plaintext; they must never reach a browser.
    cfg = {"provider": "vobiz", "credentials": {"auth_id": "MA123", "auth_token": "raw-secret"}}

    out = telephony_configs.public_telephony_config(cfg, masked=True)

    assert out["credentials"] == {"auth_id": "MA123", "auth_token": STORED_SENTINEL}


def test_secret_saved_on_a_native_row_is_masked():
    cfg = {"provider": "native", "credentials": {"auth_token": "raw-secret"}}

    assert telephony_configs.public_telephony_config(cfg, masked=True)["credentials"]["auth_token"] == STORED_SENTINEL


def test_pointer_refs_stay_visible():
    cfg = {"provider": "vobiz", "credentials": {"auth_id": "MA123", "auth_token": "env:VOBIZ_TOKEN"}}

    assert telephony_configs.public_telephony_config(cfg, masked=True)["credentials"]["auth_token"] == "env:VOBIZ_TOKEN"


async def test_startup_seals_plaintext_credentials_once(test_tenant, pool):
    config_id = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'legacy', 'vobiz', $2::jsonb) RETURNING id",
        test_tenant["id"], json.dumps({"auth_id": "MA123", "auth_token": "legacy-plaintext"}),
    )
    try:
        assert await telephony_configs.seal_plaintext_credentials(tenant_id=test_tenant["id"]) == 1
        creds = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
        assert creds["auth_token"].startswith("enc:") and decrypt_secret(creds["auth_token"]) == "legacy-plaintext"
        assert creds["auth_id"] == "MA123"

        await telephony_configs.seal_plaintext_credentials(tenant_id=test_tenant["id"])
        again = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
        assert again["auth_token"] == creds["auth_token"]
    finally:
        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config_id)


def test_native_save_seals_a_secret_named_field():
    out = telephony_configs._normalize_credentials("native", {"auth_token": "raw"}, allow_pointer_schemes=False)

    assert out["auth_token"].startswith("enc:") and decrypt_secret(out["auth_token"]) == "raw"


def test_native_save_keeps_empty_secret_fields():
    creds = {"auth_id": "", "auth_token": ""}

    assert telephony_configs._normalize_credentials("native", creds, allow_pointer_schemes=False) == creds


def test_quarantined_credential_shows_empty_so_the_admin_re_enters_it():
    cfg = {"provider": "cloudonix",
           "credentials": {"api_keys": ["quarantined", "enc:x"], "account_api_key": "quarantined"}}

    out = telephony_configs.public_telephony_config(cfg, masked=True)["credentials"]

    assert out == {"api_keys": ["", STORED_SENTINEL], "account_api_key": ""}


async def test_startup_sealing_leaves_quarantined_markers_alone(test_tenant, pool):
    # Encrypting the marker would turn "quarantined" into a working credential.
    creds = {"account_api_key": "quarantined", "api_keys": ["quarantined"], "domain": "d"}
    config_id = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'q', 'cloudonix', $2::jsonb) RETURNING id",
        test_tenant["id"], json.dumps(creds),
    )
    try:
        assert await telephony_configs.seal_plaintext_credentials(tenant_id=test_tenant["id"]) == 0
        stored = json.loads(await pool.fetchval("SELECT credentials FROM telephony_configs WHERE id = $1", config_id))
        assert stored == creds
    finally:
        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config_id)


async def test_startup_sealing_writes_a_redacted_audit_row(test_tenant, pool):
    config_id = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'legacy', 'vobiz', $2::jsonb) RETURNING id",
        test_tenant["id"], json.dumps({"auth_id": "MA1", "auth_token": "legacy-plaintext"}),
    )
    try:
        await telephony_configs.seal_plaintext_credentials(tenant_id=test_tenant["id"])
        rows = await pool.fetch(
            "SELECT old_value::text AS o, new_value::text AS n, tenant_id::text AS t FROM audit_log "
            "WHERE entity_type = 'telephony_config' AND entity_id::text = $1", str(config_id))
        assert len(rows) == 1 and "legacy-plaintext" not in rows[0]["o"] + rows[0]["n"]
        assert rows[0]["t"] == str(test_tenant["id"])
    finally:
        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config_id)
