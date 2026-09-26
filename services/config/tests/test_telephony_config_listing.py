"""The new cross-tenant `GET /telephony-configs?provider=` route (lesson 9:
scoped to this route, not the whole app) and the write-side half of the
`enc:`-only rule for `provider='cloudonix'` credentials."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import generate_key
from services.config import auth
from services.config import users as users_service
from services.config.app import app


@pytest.fixture
async def platform_client(pool):
    """role='viewer', tenant_id=None — the real shape of a platform service
    account (lesson 24), not role=='superadmin'."""
    email = f"test-platform-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="viewer", tenant_id=None,
    )
    token = auth.create_access_token(user)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as c:
            yield c
    finally:
        await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


@pytest.fixture
async def admin_client(test_admin):
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {test_admin['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture(autouse=True)
def _secret_encryption_key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


async def _create_cloudonix_config(admin_client, tenant_id: str, api_keys):
    return await admin_client.post(
        f"/tenants/{tenant_id}/telephony-configs",
        json={
            "name": "Cloudonix",
            "provider": "cloudonix",
            "credentials": {"domain": "a.cloudonix.io", "api_keys": api_keys},
        },
    )


class TestTelephonyConfigListingRoute:
    async def test_platform_service_account_gets_200(self, platform_client, admin_client, test_tenant, pool):
        create = await _create_cloudonix_config(admin_client, test_tenant["id"], ["a-real-key"])
        assert create.status_code == 201

        resp = await platform_client.get("/telephony-configs", params={"provider": "cloudonix"})
        assert resp.status_code == 200
        rows = resp.json()
        assert any(r["tenant_id"] == str(test_tenant["id"]) for r in rows)
        for r in rows:
            keys = r["credentials"]["api_keys"]
            for k in keys:
                assert k.startswith("enc:")
                assert k != "a-real-key"
                assert not k.startswith("env:")
                assert not k.startswith("k8s:")

        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", create.json()["id"])

    async def test_tenant_admin_gets_403(self, admin_client, test_tenant):
        resp = await admin_client.get("/telephony-configs", params={"provider": "cloudonix"})
        assert resp.status_code == 403


class TestCloudonixCredentialWriteValidation:
    async def test_env_ref_in_api_keys_is_400_and_writes_no_row(self, admin_client, test_tenant, pool):
        resp = await _create_cloudonix_config(admin_client, test_tenant["id"], ["env:POSTGRES_DSN"])
        assert resp.status_code == 400
        rows = await pool.fetch(
            "SELECT id FROM telephony_configs WHERE tenant_id = $1", test_tenant["id"],
        )
        assert rows == []

    async def test_k8s_ref_in_api_keys_is_400_and_writes_no_row(self, admin_client, test_tenant, pool):
        resp = await _create_cloudonix_config(admin_client, test_tenant["id"], ["k8s:/etc/passwd"])
        assert resp.status_code == 400
        rows = await pool.fetch(
            "SELECT id FROM telephony_configs WHERE tenant_id = $1", test_tenant["id"],
        )
        assert rows == []

    async def test_plaintext_key_is_stored_enc_encrypted(self, admin_client, test_tenant, pool):
        resp = await _create_cloudonix_config(admin_client, test_tenant["id"], ["a-real-key"])
        assert resp.status_code == 201
        config_id = resp.json()["id"]

        row = await pool.fetchrow("SELECT credentials FROM telephony_configs WHERE id = $1", config_id)
        stored = json.loads(row["credentials"])
        assert stored["api_keys"][0].startswith("enc:")
        assert stored["api_keys"][0] != "a-real-key"

        await pool.execute("DELETE FROM telephony_configs WHERE id = $1", config_id)
