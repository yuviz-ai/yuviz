"""Tenant checks on agent_id-keyed routes (retrieval-policy GET/PUT, knowledge-bases GET) via the ASGI app."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from services.knowledge.app import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_agent(pool, tenant):
    return dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"],
    ))


async def test_retrieval_policy_get_cross_tenant_is_403_unknown_agent_is_404(
    client, pool, test_tenant, test_admin, other_tenant_admin,
):
    # Standard convention here (unlike knowledge_bases.py): cross-tenant 403, unknown 404.
    agent = await _make_agent(pool, test_tenant)

    cross_resp = await client.get(
        f"/agents/{agent['id']}/retrieval-policy",
        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"},
    )
    unknown_resp = await client.get(
        "/agents/00000000-0000-0000-0000-000000000000/retrieval-policy",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert cross_resp.status_code == 403
    assert unknown_resp.status_code == 404


async def test_retrieval_policy_get_own_tenant_is_200(client, pool, test_tenant, test_admin):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.get(
        f"/agents/{agent['id']}/retrieval-policy",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert resp.status_code == 200


async def test_retrieval_policy_put_cross_tenant_is_403_not_written(
    client, pool, test_tenant, other_tenant_admin,
):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.put(
        f"/agents/{agent['id']}/retrieval-policy",
        json={"top_k": 3},
        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"},
    )
    assert resp.status_code == 403
    row = await pool.fetchrow("SELECT * FROM agent_retrieval_policies WHERE agent_id = $1", agent["id"])
    assert row is None


async def test_agent_kb_list_get_cross_tenant_is_403_unknown_agent_is_404(
    client, pool, test_tenant, test_admin, other_tenant_admin,
):
    agent = await _make_agent(pool, test_tenant)

    cross_resp = await client.get(
        f"/agents/{agent['id']}/knowledge-bases",
        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"},
    )
    unknown_resp = await client.get(
        "/agents/00000000-0000-0000-0000-000000000000/knowledge-bases",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert cross_resp.status_code == 403
    assert unknown_resp.status_code == 404


async def test_agent_kb_list_get_own_tenant_is_200(client, pool, test_tenant, test_admin):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.get(
        f"/agents/{agent['id']}/knowledge-bases",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []
