from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from services.config import agents, cache


@pytest_asyncio.fixture(loop_scope="session")
async def other_tenant(pool):
    """A second, independent tenant for cross-tenant isolation tests —
    test_tenant only gives us one."""
    slug = f"test-{uuid.uuid4().hex[:8]}"
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        f"Other Tenant {slug}", slug,
    )
    tenant = dict(row)
    yield tenant
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_create_and_get_agent(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
        greeting="Hi there", system_prompt="Be helpful.",
    )
    assert created["slug"] == "support-agent"
    assert created["transfer_type"] == "none"  # schema default
    assert created["config_version"] == 1

    fetched = await agents.get_agent(test_tenant["slug"], "support-agent")
    assert fetched is not None
    assert fetched["id"] == created["id"]


async def test_get_agent_returns_none_for_wrong_tenant(test_tenant, scoped):
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    assert await agents.get_agent("not-a-real-tenant-slug", "support-agent") is None


async def test_create_agent_warms_cache_when_tenant_slug_given(test_tenant, scoped):
    await cache.invalidate(f"agent:{test_tenant['slug']}:warm-test")
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="warm-test", name="Warm Test",
        tenant_slug=test_tenant["slug"],
    )
    assert await cache.get_json(f"agent:{test_tenant['slug']}:warm-test") is not None


async def test_create_agent_without_tenant_slug_does_not_warm_cache(test_tenant, scoped):
    await cache.invalidate(f"agent:{test_tenant['slug']}:no-warm-test")
    await agents.create_agent(
        tenant_id=test_tenant["id"], slug="no-warm-test", name="No Warm Test",
    )
    assert await cache.get_json(f"agent:{test_tenant['slug']}:no-warm-test") is None


async def test_update_agent_transfer_config_and_invalidates_cache(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    # Warm the cache.
    await agents.get_agent(test_tenant["slug"], "support-agent")
    assert await cache.get_json(f"agent:{test_tenant['slug']}:support-agent") is not None

    updated = await agents.update_agent(
        created["id"], tenant_slug=test_tenant["slug"],
        transfer_type="warm", transfer_destination="+18005550100", queue_id="support-queue-1",
    )
    assert updated["transfer_type"] == "warm"
    assert updated["transfer_destination"] == "+18005550100"
    assert updated["config_version"] == created["config_version"] + 1

    assert await cache.get_json(f"agent:{test_tenant['slug']}:support-agent") is None


async def test_update_agent_rejects_unknown_field(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    with pytest.raises(ValueError):
        await agents.update_agent(
            created["id"], tenant_slug=test_tenant["slug"], not_a_real_column="x",
        )


async def test_soft_delete_agent_excluded_from_get(test_tenant, scoped):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    await agents.soft_delete_agent(created["id"], tenant_slug=test_tenant["slug"])
    assert await agents.get_agent(test_tenant["slug"], "support-agent") is None


async def test_update_agent_rejects_cross_tenant_hijack(test_tenant, scoped, other_tenant):
    """Regression test for a confirmed live hijack: PATCHing an agent through
    a *different* tenant's slug must not touch it — an agent_id that exists
    but belongs to another tenant must be indistinguishable from not found."""
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Original",
    )

    with pytest.raises(LookupError):
        await agents.update_agent(
            created["id"], tenant_slug=other_tenant["slug"], name="Hijacked",
        )

    # Untouched — still visible, unchanged, under its real tenant.
    unchanged = await agents.get_agent(test_tenant["slug"], "support-agent")
    assert unchanged["name"] == "Original"


async def test_soft_delete_agent_rejects_cross_tenant_deletion(test_tenant, scoped, other_tenant):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )

    with pytest.raises(LookupError):
        await agents.soft_delete_agent(created["id"], tenant_slug=other_tenant["slug"])

    # Still alive under its real tenant.
    assert await agents.get_agent(test_tenant["slug"], "support-agent") is not None


async def test_soft_delete_agent_rejects_a_live_call(test_tenant, scoped, pool):
    # Deliberately no force override for this one (unlike the tenant/
    # provider-config guards) — this is a call happening to a real person
    # right now, not administrative housekeeping.
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    session_id = f"test-live-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, agent_id, ended_at) "
        "VALUES ($1, $2, 'inbound', $3, NULL)",
        session_id, test_tenant["slug"], created["id"],
    )
    try:
        with pytest.raises(agents.AgentHasLiveCalls) as exc_info:
            await agents.soft_delete_agent(created["id"], tenant_slug=test_tenant["slug"])
        assert exc_info.value.live_call_count == 1

        # Untouched — still there.
        assert await agents.get_agent(test_tenant["slug"], "support-agent") is not None
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_soft_delete_agent_allowed_once_call_ends(test_tenant, scoped, pool):
    created = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support-agent", name="Support",
    )
    session_id = f"test-ended-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, agent_id, ended_at) "
        "VALUES ($1, $2, 'inbound', $3, NOW())",
        session_id, test_tenant["slug"], created["id"],
    )
    try:
        await agents.soft_delete_agent(created["id"], tenant_slug=test_tenant["slug"])
        assert await agents.get_agent(test_tenant["slug"], "support-agent") is None
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
