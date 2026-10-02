"""libs/tenancy/session.py: scope precedence, and tenant_conn/platform_conn on real
Postgres (SET LOCAL reverting on a reused pooled connection can't be mocked).
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from libs.tenancy.session import (
    TenantScope,
    TenantScopeConflict,
    TenantUnresolved,
    current_scope,
    current_tenant,
    platform_conn,
    set_caller_tenant,
    set_target_tenant,
    tenant_conn,
    _scope,
)

POSTGRES_DSN = os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")


@pytest.fixture(autouse=True)
def _reset_scope():
    token = _scope.set(TenantScope())
    try:
        yield
    finally:
        _scope.reset(token)


# ── Precedence — no database needed ────────────────────────────────────────

def test_precedence_caller_wins_over_target_set_before_caller():
    set_target_tenant("target-tenant")
    set_caller_tenant("caller-tenant")
    assert current_tenant() == "caller-tenant"


def test_precedence_caller_wins_over_target_set_after_caller():
    set_caller_tenant("caller-tenant")
    set_target_tenant("target-tenant")
    assert current_tenant() == "caller-tenant"


def test_target_wins_only_when_caller_is_none():
    set_caller_tenant(None)
    set_target_tenant("target-tenant")
    assert current_tenant() == "target-tenant"


def test_tenant_scoped_caller_immovable_by_any_target_value():
    set_caller_tenant("caller-tenant")
    for target in ("other-tenant", "caller-tenant", "", None, "not-a-uuid-at-all"):
        set_target_tenant(target)
        assert current_tenant() == "caller-tenant"


def test_current_scope_reflects_both_fields():
    set_caller_tenant("c")
    set_target_tenant("t")
    assert current_scope() == TenantScope(caller="c", target="t")


# ── tenant_conn / platform_conn — real Postgres ────────────────────────────

@pytest.fixture
async def superuser_conn():
    conn = await asyncpg.connect(POSTGRES_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def tenant_row(superuser_conn):
    tid = uuid.uuid4()
    slug = f"tenant-conn-test-{tid.hex[:8]}"
    await superuser_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, 'Tenant Conn Test', $2)", tid, slug,
    )
    try:
        yield {"id": tid, "slug": slug}
    finally:
        await superuser_conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.fixture
async def pool():
    # Single connection, so the leak test reuses the same physical connection.
    p = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=1)
    try:
        yield p
    finally:
        await p.close()


async def test_pool_reuse_does_not_leak_guc_between_requests(pool, tenant_row):
    set_caller_tenant(str(tenant_row["id"]))
    async with tenant_conn(pool) as conn:
        value = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
        assert value == str(tenant_row["id"])

    # Same connection, no scope: SET LOCAL must have reverted.
    _scope.set(TenantScope())
    async with pool.acquire() as conn:
        leaked = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
        assert leaked in ("", None)


async def test_fail_closed_raises_tenant_unresolved_before_yielding(pool):
    set_caller_tenant(None)
    set_target_tenant(None)
    with pytest.raises(TenantUnresolved):
        async with tenant_conn(pool):
            pytest.fail("tenant_conn must raise before yielding a connection")


async def test_explicit_tenant_without_reason_raises_value_error(pool):
    with pytest.raises(ValueError):
        async with tenant_conn(pool, explicit_tenant="some-tenant"):
            pytest.fail("must raise before yielding")


async def test_explicit_tenant_conflicting_with_caller_raises_scope_conflict(pool, tenant_row):
    set_caller_tenant(str(tenant_row["id"]))
    other = str(uuid.uuid4())
    with pytest.raises(TenantScopeConflict):
        async with tenant_conn(pool, explicit_tenant=other, reason="test-conflict"):
            pytest.fail("must raise before yielding")


async def test_explicit_tenant_matching_caller_is_allowed(pool, tenant_row):
    set_caller_tenant(str(tenant_row["id"]))
    async with tenant_conn(pool, explicit_tenant=str(tenant_row["id"]), reason="test-match") as conn:
        value = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
        assert value == str(tenant_row["id"])


async def test_explicit_tenant_accepts_a_raw_uuid_object_not_just_a_string(pool, tenant_row):
    # asyncpg returns uuid.UUID objects; they must not fall into the slug branch.
    set_caller_tenant(tenant_row["id"])
    async with tenant_conn(pool) as conn:
        value = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
        assert value == str(tenant_row["id"])


async def test_platform_conn_stamp_tenant_accepts_a_raw_uuid_object(pool, tenant_row):
    async with platform_conn(pool, reason="test-stamp-uuid-object", stamp_tenant=tenant_row["id"]) as conn:
        value = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
        assert value == str(tenant_row["id"])


async def test_platform_conn_bypasses_and_reverts_role(pool):
    async with platform_conn(pool, reason="test-bypass") as conn:
        role = await conn.fetchval("SELECT current_setting('role')")
        assert role == "yuviz_platform"

    async with pool.acquire() as conn:
        role = await conn.fetchval("SELECT current_setting('role')")
        assert role != "yuviz_platform"
