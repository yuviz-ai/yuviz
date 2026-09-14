"""
DB-backed tests for services/toolexec/agent_apis.py (T8) — the AC 10
write-time authorization gate and the chain-depth enable-time gate.
"""

from __future__ import annotations

import uuid

import pytest

from services.config.auth import CurrentUser
from services.toolexec import agent_apis


def _admin(tenant_id: str) -> CurrentUser:
    return CurrentUser(id=str(uuid.uuid4()), email="admin@test.example", role="admin", tenant_id=tenant_id)


async def _make_tenant(pool, slug: str) -> dict:
    return dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"T {slug}", slug,
    ))


async def _make_custom_api(pool, tenant_id, name: str, chain_levels: int = 1) -> dict:
    return dict(await pool.fetchrow(
        "INSERT INTO custom_apis (tenant_id, name, description, endpoint_url, method, chain_levels) "
        "VALUES ($1, $2, 'd', 'https://example.com/api', 'GET', $3) RETURNING *",
        tenant_id, name, chain_levels,
    ))


@pytest.mark.asyncio
async def test_cross_tenant_custom_api_id_404s_byte_identical_to_random_uuid(pool, tenant_agent):
    tenant, agent = tenant_agent
    other_tenant = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:8]}")
    other_api = await _make_custom_api(pool, other_tenant["id"], "other_api")
    caller = _admin(str(tenant["id"]))

    try:
        with pytest.raises(LookupError) as cross_tenant_exc:
            await agent_apis.set_enabled(agent["id"], other_api["id"], enabled=True, current_user=caller)

        with pytest.raises(LookupError) as random_uuid_exc:
            await agent_apis.set_enabled(agent["id"], str(uuid.uuid4()), enabled=True, current_user=caller)

        # Byte-identical detail (lesson 2) — a divergence in either the
        # status class or the text would let a tenant-A admin distinguish
        # "exists elsewhere" from "does not exist at all".
        assert str(cross_tenant_exc.value) == str(random_uuid_exc.value)

        rows = await pool.fetch("SELECT * FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
        assert rows == []
    finally:
        await pool.execute("DELETE FROM custom_apis WHERE id = $1", other_api["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


@pytest.mark.asyncio
async def test_wrong_tenant_caller_404s_byte_identical_to_random_uuid(pool, tenant_agent):
    """Distinct from the cross-tenant custom_api_id case above: here the
    agent AND the custom_api both genuinely belong to the SAME tenant (the
    SQL JOIN's own ca.tenant_id = a.tenant_id condition is satisfied), but
    the CALLER is a tenant-B admin acting on tenant A's own agent. Only
    the Python-side `row["tenant_id"] != current_user.tenant_id` check
    catches this — the JOIN condition alone cannot, since nothing in this
    query involves the caller's identity."""
    tenant, agent = tenant_agent
    api = await _make_custom_api(pool, tenant["id"], f"own_tenant_api_{uuid.uuid4().hex[:8]}")
    other_tenant = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:8]}")
    wrong_tenant_caller = _admin(str(other_tenant["id"]))

    try:
        with pytest.raises(LookupError) as wrong_tenant_exc:
            await agent_apis.set_enabled(agent["id"], api["id"], enabled=True, current_user=wrong_tenant_caller)

        with pytest.raises(LookupError) as random_uuid_exc:
            await agent_apis.set_enabled(
                str(uuid.uuid4()), str(uuid.uuid4()), enabled=True, current_user=wrong_tenant_caller,
            )

        assert str(wrong_tenant_exc.value) == str(random_uuid_exc.value)

        rows = await pool.fetch("SELECT * FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
        assert rows == []
    finally:
        await pool.execute("DELETE FROM custom_apis WHERE id = $1", api["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


@pytest.mark.asyncio
async def test_enable_rejected_when_chain_levels_exceeds_effective_ceiling(pool, tenant_agent):
    tenant, agent = tenant_agent
    api = await _make_custom_api(pool, tenant["id"], "deep_api", chain_levels=3)
    tpc = dict(await pool.fetchrow(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine) "
        "VALUES ($1, 'x', 'execute_api', 'toolexec') RETURNING *", tenant["id"],
    ))
    await pool.execute(
        "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id, max_chain_depth) "
        "VALUES ($1, 'execute_api', $2, 2)", agent["id"], tpc["id"],
    )

    try:
        caller = _admin(str(tenant["id"]))
        with pytest.raises(ValueError, match="chain_depth_exceeds_agent_ceiling"):
            await agent_apis.set_enabled(agent["id"], api["id"], enabled=True, current_user=caller)

        rows = await pool.fetch("SELECT * FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
        assert rows == []
    finally:
        await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id = $1", agent["id"])
        await pool.execute("DELETE FROM tool_provider_configs WHERE id = $1", tpc["id"])


@pytest.mark.asyncio
async def test_enable_succeeds_within_effective_ceiling(pool, tenant_agent):
    """Control: the SAME shape as the rejection test above, one level
    shallower, must be admitted — proving the ceiling check is exact, not
    a rejection that fires unconditionally."""
    tenant, agent = tenant_agent
    api = await _make_custom_api(pool, tenant["id"], "shallow_api", chain_levels=2)
    tpc = dict(await pool.fetchrow(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine) "
        "VALUES ($1, 'x', 'execute_api', 'toolexec') RETURNING *", tenant["id"],
    ))
    await pool.execute(
        "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id, max_chain_depth) "
        "VALUES ($1, 'execute_api', $2, 2)", agent["id"], tpc["id"],
    )

    try:
        caller = _admin(str(tenant["id"]))
        result = await agent_apis.set_enabled(agent["id"], api["id"], enabled=True, current_user=caller)
        assert result["enabled"] is True

        rows = await pool.fetch("SELECT * FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
        assert len(rows) == 1
    finally:
        await pool.execute("DELETE FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
        await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id = $1", agent["id"])
        await pool.execute("DELETE FROM tool_provider_configs WHERE id = $1", tpc["id"])
