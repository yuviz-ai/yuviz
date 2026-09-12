"""
HTTP-layer tests for GET /knowledge-bases/{kb_id}/agents — the reverse
lookup's caller-tenant predicate, exercised through the real ASGI app (same
convention as services/did/tests/test_numbers_api.py).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import knowledge_bases as kb_service
from services.knowledge.app import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_kb(tenant):
    return await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="policies", name="Policies")


async def test_own_tenant_admin_gets_200_with_agents(client, pool, test_tenant, test_admin):
    kb = await _make_kb(test_tenant)
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", test_tenant["id"],
    ))
    await agent_kb_service.assign(agent["id"], kb["id"])

    resp = await client.get(
        f"/knowledge-bases/{kb['id']}/agents",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [row["agent_id"] for row in body] == [str(agent["id"])]


async def test_cross_tenant_admin_gets_same_404_as_unknown_id(client, test_tenant, test_admin, other_tenant_admin):
    kb = await _make_kb(test_tenant)

    cross_resp = await client.get(
        f"/knowledge-bases/{kb['id']}/agents",
        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"},
    )
    unknown_resp = await client.get(
        "/knowledge-bases/00000000-0000-0000-0000-000000000000/agents",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )

    assert cross_resp.status_code == 404
    assert unknown_resp.status_code == 404
    assert cross_resp.json()["detail"] == unknown_resp.json()["detail"].replace(
        "00000000-0000-0000-0000-000000000000", str(kb["id"]),
    )


async def test_null_tenant_service_viewer_gets_200(client, test_tenant, test_service_viewer):
    kb = await _make_kb(test_tenant)

    resp = await client.get(
        f"/knowledge-bases/{kb['id']}/agents",
        headers={"Authorization": f"Bearer {test_service_viewer['token']}"},
    )
    assert resp.status_code == 200


async def test_own_tenant_viewer_gets_200(client, test_tenant, test_viewer):
    kb = await _make_kb(test_tenant)

    resp = await client.get(
        f"/knowledge-bases/{kb['id']}/agents",
        headers={"Authorization": f"Bearer {test_viewer['token']}"},
    )
    assert resp.status_code == 200


async def test_no_authorization_header_is_401(client, test_tenant):
    kb = await _make_kb(test_tenant)

    resp = await client.get(f"/knowledge-bases/{kb['id']}/agents")
    assert resp.status_code == 401


async def test_malformed_kb_id_is_404_not_500(client, test_admin):
    resp = await client.get(
        "/knowledge-bases/not-a-uuid/agents",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert resp.status_code == 404
