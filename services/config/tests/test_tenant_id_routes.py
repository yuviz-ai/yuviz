"""A tenant id that is not a UUID, or names no tenant, is a 404 on the /tenants/{id} write routes (#165)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from services.config.app import app

BAD_IDS = ["nope", "not-a-uuid", "123", "x%20y", "0" * 40, "'; DROP TABLE tenants;--"]
ROUTES = [
    ("PATCH", "", {"name": "renamed"}),
    ("PATCH", "/concurrency", {"max_concurrent_calls": 3}),
    ("PATCH", "/transfer-policy", {"allow_international_transfers": True}),
    ("DELETE", "", None),
]


def _client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _call(c, method, tenant_id, suffix, body):
    return await c.request(method, f"/tenants/{tenant_id}{suffix}", **({} if body is None else {"json": body}))


@pytest.mark.parametrize("method,suffix,body", ROUTES)
@pytest.mark.parametrize("tenant_id", BAD_IDS)
async def test_a_non_uuid_tenant_id_is_404_not_500(test_superadmin, method, suffix, body, tenant_id):
    async with _client(test_superadmin["token"]) as c:
        resp = await _call(c, method, tenant_id, suffix, body)
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("method,suffix,body", ROUTES)
async def test_a_well_formed_but_unknown_tenant_id_is_404(test_superadmin, method, suffix, body):
    async with _client(test_superadmin["token"]) as c:
        resp = await _call(c, method, uuid.uuid4(), suffix, body)
    assert resp.status_code == 404, resp.text


async def test_the_malformed_id_check_leaves_role_gating_first(test_viewer, test_admin):
    """A viewer is still refused by the role dependency; an admin cannot use /tenants/{id} or transfer-policy."""
    async with _client(test_viewer["token"]) as c:
        assert (await _call(c, "PATCH", "nope", "", {"name": "x"})).status_code == 403
    async with _client(test_admin["token"]) as c:
        assert (await _call(c, "PATCH", "nope", "", {"name": "x"})).status_code == 403
        assert (await _call(c, "PATCH", "nope", "/transfer-policy", {"allow_international_transfers": True})).status_code == 403
        assert (await _call(c, "DELETE", "nope", "", None)).status_code == 403


async def test_a_real_tenant_id_still_updates(test_superadmin, test_tenant):
    async with _client(test_superadmin["token"]) as c:
        resp = await _call(c, "PATCH", test_tenant["id"], "/concurrency", {"max_concurrent_calls": 7})
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_concurrent_calls"] == 7
