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


async def test_update_tenant_bumps_config_version_and_rewrites_cache(test_tenant):
    slug = test_tenant["slug"]
    tenant_id = test_tenant["id"]

    # Warm the cache first.
    await tenants.get_tenant(slug)
    assert await cache.get_json(f"tenant:{slug}") is not None

    updated = await tenants.update_tenant(tenant_id, vad_hold_ms=500)
    assert updated["vad_hold_ms"] == 500
    assert updated["config_version"] == test_tenant["config_version"] + 1

    # The Gateway reads tenant:{slug} and never Postgres: the write leaves the NEW row there,
    # with no expiry, rather than a gap that puts calls back on gateway defaults.
    cached = await cache.get_json(f"tenant:{slug}")
    assert cached["vad_hold_ms"] == 500
    assert cached["config_version"] == updated["config_version"]
    assert await cache.get_client().ttl(f"tenant:{slug}") == -1


async def test_prewarm_tenants_restores_a_flushed_key(test_tenant):
    slug = test_tenant["slug"]
    await cache.invalidate(f"tenant:{slug}")
    await tenants.prewarm_tenants(overwrite=False)
    cached = await cache.get_json(f"tenant:{slug}")
    assert cached is not None and str(cached["id"]) == str(test_tenant["id"])
    assert await cache.get_client().ttl(f"tenant:{slug}") == -1


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
        # Not changed_at: now() is transaction start, so the update that waited on the lock can sort first.
        "AND action = 'updated' ORDER BY id",
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


async def test_soft_delete_tenant_disconnects_its_oauth_connections(test_tenant, pool):
    tenant_id = test_tenant["id"]
    await pool.execute(
        "INSERT INTO oauth_connections (tenant_id, provider, status, access_token_ref, access_expires_at, "
        "                               refresh_token_ref, auth_kind) "
        "VALUES ($1, 'google', 'connected', 'enc:t1.a', now() + interval '1 hour', 'enc:t1.r', 'oauth2')", tenant_id,
    )
    try:
        await tenants.soft_delete_tenant(tenant_id, force=True)
        row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE tenant_id = $1", tenant_id)
        assert (row["status"], row["access_token_ref"], row["refresh_token_ref"]) == ("disconnected", None, None)
    finally:
        await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = $1", tenant_id)
