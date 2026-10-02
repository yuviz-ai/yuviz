from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from services.config import cache, db, tenants


async def test_create_and_get_tenant():
    slug = f"test-{uuid.uuid4().hex[:8]}"
    try:
        created = await tenants.create_tenant(name="Test Co", slug=slug, region="in")
        assert created["slug"] == slug
        assert created["region"] == "in"
        assert created["config_version"] == 1

        fetched = await tenants.get_tenant(slug)
        assert fetched is not None
        assert fetched["id"] == created["id"]
    finally:
        await cache.invalidate(f"tenant:{slug}")
        pool = await db.get_pool()
        await pool.execute("DELETE FROM tenants WHERE slug = $1", slug)


async def test_get_tenant_is_cached_on_second_read(test_tenant):
    slug = test_tenant["slug"]

    # First call misses cache, populates it.
    first = await tenants.get_tenant(slug)
    assert first is not None

    cached = await cache.get_json(f"tenant:{slug}")
    assert cached is not None
    assert cached["slug"] == slug


async def test_update_tenant_bumps_config_version_and_invalidates_cache(test_tenant):
    slug = test_tenant["slug"]
    tenant_id = test_tenant["id"]

    # Warm the cache first.
    await tenants.get_tenant(slug)
    assert await cache.get_json(f"tenant:{slug}") is not None

    updated = await tenants.update_tenant(tenant_id, vad_hold_ms=500)
    assert updated["vad_hold_ms"] == 500
    assert updated["config_version"] == test_tenant["config_version"] + 1

    # Cache must be invalidated by the write, not just left stale for 60s.
    assert await cache.get_json(f"tenant:{slug}") is None


async def test_update_tenant_rejects_unknown_field(test_tenant):
    with pytest.raises(ValueError):
        await tenants.update_tenant(test_tenant["id"], not_a_real_column="x")


async def test_concurrent_updates_do_not_produce_stale_audit_old_value(test_tenant, pool):
    """Racing updates: the second audit row's old_value equals the first's new_value."""
    await asyncio.gather(
        tenants.update_tenant(test_tenant["id"], vad_hold_ms=100),
        tenants.update_tenant(test_tenant["id"], vad_hold_ms=200),
    )

    audit_rows = await pool.fetch(
        "SELECT * FROM audit_log WHERE entity_type = 'tenant' AND entity_id = $1 "
        "AND action = 'updated' ORDER BY changed_at",
        test_tenant["id"],
    )
    assert len(audit_rows) == 2

    first_new_hold_ms = json.loads(audit_rows[0]["new_value"])["vad_hold_ms"]
    second_old_hold_ms = json.loads(audit_rows[1]["old_value"])["vad_hold_ms"]
    assert second_old_hold_ms == first_new_hold_ms


async def test_update_tenant_writes_audit_row(test_tenant, pool):
    await tenants.update_tenant(test_tenant["id"], region="eu")

    row = await pool.fetchrow(
        "SELECT * FROM audit_log WHERE entity_type = 'tenant' AND entity_id = $1 "
        "ORDER BY changed_at DESC LIMIT 1",
        test_tenant["id"],
    )
    assert row is not None
    assert row["action"] == "updated"
    assert row["new_value"] is not None


async def test_update_tenant_accepts_max_concurrent_calls(test_tenant):
    updated = await tenants.update_tenant(test_tenant["id"], max_concurrent_calls=5)
    assert updated["max_concurrent_calls"] == 5
