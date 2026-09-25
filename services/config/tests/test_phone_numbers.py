from __future__ import annotations

import uuid

import pytest

from services.config import agents, cache, phone_numbers


async def test_create_and_get_by_did_resolves_tenant_and_agent(test_tenant, scoped, pool):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support", name="Support Agent",
    )
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did, agent_id=agent["id"],
    )

    resolved = await phone_numbers.get_by_did(did)
    assert resolved == {
        "tenant_slug": test_tenant["slug"], "agent_slug": "support", "version": 1,
    }

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_with_no_agent_resolves_to_default_agent_slug(test_tenant, scoped, pool):
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did, agent_id=None)

    resolved = await phone_numbers.get_by_did(did)
    assert resolved == {
        "tenant_slug": test_tenant["slug"], "agent_slug": "default", "version": None,
    }

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_returns_agent_config_version(test_tenant, scoped, pool):
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="sales", name="Sales Agent",
    )
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did, agent_id=agent["id"],
    )

    assert (await phone_numbers.get_by_did(did))["version"] == 1

    # An agent update bumps config_version via the existing trigger — a
    # fresh (post-invalidation) resolution must reflect the new version.
    await agents.update_agent(agent["id"], tenant_slug=test_tenant["slug"], greeting="Updated greeting")
    await cache.invalidate(f"did:{did}")

    assert (await phone_numbers.get_by_did(did))["version"] == 2

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_unknown_number_returns_none(test_tenant, scoped):
    assert await phone_numbers.get_by_did("test-did-does-not-exist") is None


async def test_create_phone_number_warms_cache_immediately(test_tenant, scoped, pool):
    # create_phone_number() warms did:{did} itself now — a brand-new DID
    # must not sit cold until its first real call (or some unrelated read)
    # happens to populate it; see project memory 2026-07-13 for the real
    # misrouted call this exact gap caused before this fix.
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    assert await cache.get_json(f"did:{did}") is not None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_get_by_did_populates_cache_on_miss(test_tenant, scoped, pool):
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    await cache.invalidate(f"did:{did}")  # force a miss, independent of create's own warming

    assert await cache.get_json(f"did:{did}") is None
    await phone_numbers.get_by_did(did)
    assert await cache.get_json(f"did:{did}") is not None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_update_phone_number_rename_updates_did_and_clears_old_row(test_tenant, scoped, pool):
    """Superseded by test_update_phone_number_rename_writes_through_new_and_clears_old
    for cache behavior (write-through, not invalidate-only, is the current
    design) — kept for the plain row-level rename assertion."""
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    new_did = f"test-did-{uuid.uuid4().hex[:8]}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    updated = await phone_numbers.update_phone_number(created["id"], did=new_did)
    assert updated["did"] == new_did

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", new_did)


async def test_soft_delete_phone_number_invalidates_cache_and_hides_from_get_by_did(test_tenant, scoped, pool):
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    await phone_numbers.get_by_did(did)  # warm cache

    await phone_numbers.soft_delete_phone_number(created["id"])
    assert await cache.get_json(f"did:{did}") is None
    assert await phone_numbers.get_by_did(did) is None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_did_unique_constraint_rejects_duplicate(test_tenant, scoped, pool):
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
    with pytest.raises(Exception):
        await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_list_phone_numbers_scoped_to_tenant(test_tenant, scoped, pool):
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    listed = await phone_numbers.list_phone_numbers(test_tenant["id"])
    assert [p["did"] for p in listed] == [did]

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_inactive_status_resolves_to_none_not_normal_agent(test_tenant, scoped, pool):
    """A suspended/inactive DID must route like an unrecognized number, not
    keep resolving to its normal agent just because the row still exists."""
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="support", name="Support Agent",
    )
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did, agent_id=agent["id"], status="suspended",
    )

    assert await phone_numbers.get_by_did(did) is None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_fallback_agent_used_when_primary_agent_deleted(test_tenant, scoped, pool):
    primary = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="sales", name="Sales Agent",
    )
    fallback = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="receptionist", name="Receptionist Agent",
    )
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did,
        agent_id=primary["id"], fallback_agent_id=fallback["id"],
    )

    # Primary agent still active — resolves to it, not the fallback.
    resolved = await phone_numbers.get_by_did(did)
    assert resolved["agent_slug"] == "sales"

    # Soft-delete the primary agent and force a fresh resolution (bypass
    # cache — this test is about the SQL fallback logic, not TTL/invalidation,
    # which is already covered elsewhere).
    await pool.execute("UPDATE agents SET deleted_at = now() WHERE id = $1", primary["id"])
    await cache.invalidate(f"did:{did}")

    resolved = await phone_numbers.get_by_did(did)
    assert resolved["agent_slug"] == "receptionist"

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_fallback_agent_used_when_primary_agent_deactivated(test_tenant, scoped, pool):
    """Same fallback behavior as a deleted primary agent, but for the
    reversible status='inactive' case instead of deleted_at."""
    primary = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="sales2", name="Sales Agent 2",
    )
    fallback = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="receptionist2", name="Receptionist Agent 2",
    )
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(
        tenant_id=test_tenant["id"], did=did,
        agent_id=primary["id"], fallback_agent_id=fallback["id"],
    )

    resolved = await phone_numbers.get_by_did(did)
    assert resolved["agent_slug"] == "sales2"

    await pool.execute("UPDATE agents SET status = 'inactive' WHERE id = $1", primary["id"])
    await cache.invalidate(f"did:{did}")

    resolved = await phone_numbers.get_by_did(did)
    assert resolved["agent_slug"] == "receptionist2"

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_status_check_constraint_rejects_invalid_value(test_tenant, scoped, pool):
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    with pytest.raises(Exception):
        await phone_numbers.create_phone_number(
            tenant_id=test_tenant["id"], did=did, status="not-a-real-status",
        )


async def test_prewarm_populates_cache_for_active_dids_only(test_tenant, scoped, pool):
    active_did = f"test-did-{uuid.uuid4().hex[:8]}"
    inactive_did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=active_did, status="active")
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=inactive_did, status="inactive")

    await cache.invalidate(f"did:{active_did}", f"did:{inactive_did}")
    warmed = await phone_numbers.prewarm()

    assert warmed >= 1  # at least this test's active DID (other tests may leave rows too)
    assert await cache.get_json(f"did:{active_did}") is not None
    assert await cache.get_json(f"did:{inactive_did}") is None  # prewarm only queries active rows

    await pool.execute("DELETE FROM phone_numbers WHERE did IN ($1, $2)", active_did, inactive_did)


async def test_did_cache_has_no_ttl(test_tenant, scoped, pool):
    """Canonical design (project memory 2026-07-14): DID routing entries
    never expire — the Gateway's Redis-only hot path has no fallback query,
    so any TTL is a live landmine, not just a tuning knob. Every write path
    keeps Redis in lockstep with Postgres instead (see this module's
    top-of-file comment)."""
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    client = cache.get_client()
    assert await client.ttl(f"did:{did}") == -1  # -1 == "no expiry", per redis-py

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_update_phone_number_writes_through_new_did_immediately(test_tenant, scoped, pool):
    """update_phone_number() must repopulate the cache itself, not just
    invalidate it — with no TTL, a plain invalidate() would leave the DID
    completely unroutable until some unrelated read happened to refresh it,
    which for an admin-triggered edit outside a live call could be a long
    time (see this module's top-of-file comment)."""
    agent = await agents.create_agent(
        tenant_id=test_tenant["id"], slug="write-through-agent", name="Write Through",
    )
    did = f"test-did-{uuid.uuid4().hex[:8]}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)

    await phone_numbers.update_phone_number(created["id"], agent_id=agent["id"])

    cached = await cache.get_json(f"did:{did}")
    assert cached is not None
    assert cached["agent_slug"] == "write-through-agent"

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)


async def test_update_phone_number_rename_writes_through_new_and_clears_old(test_tenant, scoped, pool):
    old_did = f"test-did-{uuid.uuid4().hex[:8]}"
    new_did = f"test-did-{uuid.uuid4().hex[:8]}"
    created = await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=old_did)

    await phone_numbers.update_phone_number(created["id"], did=new_did)

    assert await cache.get_json(f"did:{old_did}") is None
    assert await cache.get_json(f"did:{new_did}") is not None

    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", new_did)
