"""
HTTP-layer regression tests for two routers found with no tenant check at
all while auditing RLS Tier 3 coverage (T21/T36's coverage tripwire
surfaced both as entirely absent from the design's own Tier 3 table):

- GET/PUT /agents/{agent_id}/retrieval-policy (retrieval_policies.py)
- GET /agents/{agent_id}/knowledge-bases (agent_kb.py's list route —
  PATCH/DELETE on this router already had the check; GET did not)

Same convention as test_kb_agents_api.py: exercised through the real ASGI
app, not the service layer directly, since the bug was in the router.
"""

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
    # assert_tenant_access's standard convention (not knowledge_bases.py's
    # deliberate 404-only anti-oracle exception): a genuine cross-tenant
    # UUID mismatch is 403, an unresolvable/unknown id is 404 — matching
    # agent_kb.py's already-shipped PATCH/DELETE on the same _authorize_agent.
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
