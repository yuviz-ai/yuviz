"""
Runs against the real local Postgres + Redis (same as the rest of this repo's
tests-that-touch-infra convention — see services/conversation/transcript_builder.py).
Not mocked: these tests exist to prove the cache-aside + same-transaction-audit
pattern actually works against real Postgres semantics (triggers, FKs,
transactions), which a mock would happily let slide past.

Every test tenant/agent/provider_config uses a slug prefixed 'test-' and is
hard-deleted (not soft-deleted) at teardown — these are throwaway fixture
rows, not data anyone needs an audit trail for.
"""

from __future__ import annotations

import os
import uuid

import pytest_asyncio

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)

from services.config import auth, cache, db  # noqa: E402  (env defaults must land first)
from services.config import users as users_service  # noqa: E402
from libs.tenancy import set_target_tenant  # noqa: E402


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await db.get_pool()
    yield p
    # Pool is process-wide by design (db.py), shared across the whole test
    # session (see asyncio_default_fixture_loop_scope in pyproject.toml) —
    # not closed per-test. Left open at process exit; the OS reclaims the
    # sockets, which is fine for a test run.


@pytest_asyncio.fixture(loop_scope="session")
async def test_tenant(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        f"Test Tenant {slug}", slug,
    )
    tenant = dict(row)
    yield tenant
    await cache.invalidate(f"tenant:{slug}")
    # Cascade manually — agents/provider_configs/phone_numbers/
    # tool_provider_configs FK-reference tenants without ON DELETE CASCADE
    # (a real delete in production should be a deliberate, audited action
    # per entity, not an implicit cascade), so test cleanup has to clear
    # dependents itself before the tenant row can go. agent_tool_policies
    # references both agents and tool_provider_configs, so it must go
    # first, before either of those.
    await pool.execute(
        "DELETE FROM agent_tool_policies WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        tenant["id"],
    )
    # purchased_numbers references tenants/carriers/phone_numbers — must go
    # before all three.
    await pool.execute("DELETE FROM purchased_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tool_provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM carriers WHERE tenant_id = $1", tenant["id"])
    # test_admin (soft-deleted, not hard-deleted, by its own teardown — see
    # its fixture comment) still FK-references this tenant at this point,
    # and can't be hard-deleted itself (it's an audit_log actor too, same
    # constraint as test_superadmin). Detach it instead of deleting it.
    await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture
async def scoped(test_tenant):
    """Sets the ambient RLS tenant scope (libs.tenancy) for tests that call
    services/config functions directly, bypassing the HTTP layer that
    normally sets it via deps.get_authenticated_user/bind_path_tenant —
    without this, tenant_conn() raises TenantUnresolved (rls-tenant-isolation).
    Reset to None after: the scope ContextVar is set on the
    ambient context, and this suite's tests share one event loop
    (asyncio_default_fixture_loop_scope), so a value left behind would leak
    into whichever test runs next."""
    set_target_tenant(str(test_tenant["id"]))
    yield
    set_target_tenant(None)


@pytest_asyncio.fixture(loop_scope="session")
async def test_superadmin(pool):
    """A real superadmin user + a real signed token, created fresh per test
    (function-scoped like test_tenant, despite loop_scope="session" only
    pinning the event loop, not fixture caching) — most routes require auth
    now (see deps.py)."""
    email = f"test-superadmin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(email=email, password="test-password-not-real", role="superadmin")
    token = auth.create_access_token(user)
    yield {"user": user, "token": token}
    # Soft-delete, not a hard DELETE: this user is the actor on every
    # audit_log row the test just wrote (real user_id now flows through, see
    # every router's require_role()/get_current_user() usage) — audit_log's
    # user_id FK correctly refuses to let a referenced user be hard-deleted.
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_admin(pool, test_tenant):
    """Tenant-scoped admin (not superadmin) — for asserting the create_user()
    escalation guard: can create admin/viewer users in its own tenant, not
    superadmins or users in another tenant. Soft-deleted like test_superadmin,
    not hard-deleted like test_viewer: unlike a pure viewer, an admin can
    perform real audited writes (e.g. create_user()), which leave it as the
    actor on an audit_log row — the same FK constraint test_superadmin's
    comment explains."""
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="admin", tenant_id=test_tenant["id"],
    )
    token = auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_viewer(pool, test_tenant):
    """A non-privileged user, scoped to test_tenant, for asserting 403s on
    role-restricted endpoints. Hard-deleted (not soft), unlike
    test_superadmin: a viewer is read-only by construction, so it never
    performs a successful (audited) write that would leave an audit_log row
    referencing it as actor — and it must be fully gone, not just
    soft-deleted, before test_tenant's own teardown hard-deletes the tenant
    row this user's tenant_id FK-references."""
    email = f"test-viewer-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="viewer", tenant_id=test_tenant["id"],
    )
    token = auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("DELETE FROM users WHERE id = $1", user["id"])
