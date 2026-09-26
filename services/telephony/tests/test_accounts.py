from __future__ import annotations

import pytest

from libs.config_sdk.secrets import encrypt_secret
from services.telephony import accounts as accounts_module


def _fake_get(rows_by_provider: dict[str, list[dict]]):
    async def _get(self, path, params=None):
        if path == "/telephony-configs":
            return rows_by_provider.get(params["provider"], [])
        if path.startswith("/tenants/") and path.endswith("/agents"):
            return []
        raise AssertionError(f"unexpected path {path!r}")
    return _get


@pytest.mark.asyncio
async def test_scalar_and_list_sensitive_fields_both_decrypt(monkeypatch):
    store = accounts_module.AccountStore()
    vobiz_token = encrypt_secret("real-auth-token")
    cx_key = encrypt_secret("real-api-key")

    rows = {
        "vobiz": [{
            "id": "cfg-vobiz", "tenant_id": "t-1", "tenant_slug": "tenant-a",
            "provider": "vobiz", "is_default_outbound": True,
            "credentials": {"auth_id": "aid", "auth_token": vobiz_token},
        }],
        "cloudonix": [{
            "id": "cfg-cx", "tenant_id": "t-2", "tenant_slug": "tenant-b",
            "provider": "cloudonix", "is_default_outbound": False,
            "credentials": {"domain": "b.cloudonix.io", "api_keys": [cx_key]},
        }],
        "fake": [],
    }
    monkeypatch.setattr(accounts_module.AccountStore, "_get", _fake_get(rows))
    await store.refresh()

    assert store.loaded is True
    vobiz_account = store.get("vobiz", "cfg-vobiz")
    assert vobiz_account.credentials["auth_token"] == "real-auth-token"
    cx_account = store.get("cloudonix", "cfg-cx")
    assert cx_account.credentials["api_keys"] == ["real-api-key"]
    assert store.default_outbound_for("tenant-a") is vobiz_account
    assert store.default_outbound_for("tenant-b") is None


@pytest.mark.asyncio
async def test_plaintext_legacy_value_passes_through_with_warning(monkeypatch, caplog):
    store = accounts_module.AccountStore()
    rows = {
        "vobiz": [{
            "id": "cfg-legacy", "tenant_id": "t-1", "tenant_slug": "tenant-a",
            "provider": "vobiz", "is_default_outbound": False,
            "credentials": {"auth_id": "aid", "auth_token": "plaintext-legacy-token"},
        }],
        "cloudonix": [], "fake": [],
    }
    monkeypatch.setattr(accounts_module.AccountStore, "_get", _fake_get(rows))
    with caplog.at_level("WARNING"):
        await store.refresh()

    account = store.get("vobiz", "cfg-legacy")
    assert account.credentials["auth_token"] == "plaintext-legacy-token"
    assert any("non-enc" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_undecryptable_entry_skipped_without_failing_refresh(monkeypatch):
    store = accounts_module.AccountStore()
    rows = {
        "cloudonix": [{
            "id": "cfg-bad", "tenant_id": "t-1", "tenant_slug": "tenant-a",
            "provider": "cloudonix", "is_default_outbound": False,
            "credentials": {"domain": "a.cloudonix.io", "api_keys": ["enc:corrupted-or-wrong-key"]},
        }],
        "vobiz": [], "fake": [],
    }
    monkeypatch.setattr(accounts_module.AccountStore, "_get", _fake_get(rows))
    await store.refresh()

    assert store.loaded is True
    account = store.get("cloudonix", "cfg-bad")
    assert account is not None
    assert account.credentials["api_keys"] == []


@pytest.mark.asyncio
async def test_failed_config_fetch_keeps_last_known_good(monkeypatch):
    store = accounts_module.AccountStore()
    good_token = encrypt_secret("real-auth-token")
    rows = {
        "vobiz": [{
            "id": "cfg-a", "tenant_id": "t-1", "tenant_slug": "tenant-a",
            "provider": "vobiz", "is_default_outbound": True,
            "credentials": {"auth_id": "aid", "auth_token": good_token},
        }],
        "cloudonix": [], "fake": [],
    }
    monkeypatch.setattr(accounts_module.AccountStore, "_get", _fake_get(rows))
    await store.refresh()
    assert store.get("vobiz", "cfg-a") is not None

    async def _failing_get(self, path, params=None):
        raise RuntimeError("config service unreachable")

    monkeypatch.setattr(accounts_module.AccountStore, "_get", _failing_get)
    await store.refresh()
    assert store.loaded is True
    assert store.get("vobiz", "cfg-a") is not None
