"""Fixtures against real local Postgres + Redis (not mocked).

Test rows use a 'test-' slug prefix and are hard-deleted at teardown.
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
    # Process-wide pool shared across the session; intentionally never closed.


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
    # did:{did} has no TTL and the row deletes below never touch Redis, so
    # routes written during the test would otherwise outlive it forever.
    async for key in cache.get_client().scan_iter(match="did:*"):
        route = await cache.get_json(key)
        if route and route.get("tenant_slug") == slug:
            await cache.invalidate(key)
    # No ON DELETE CASCADE on tenant FKs, so delete dependents in FK order.
    await pool.execute(
        "DELETE FROM agent_tool_policies WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        tenant["id"],
    )
    await pool.execute("DELETE FROM purchased_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tool_provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute(
        "UPDATE tenants SET default_stt_config_id = NULL, default_llm_config_id = NULL, "
        "default_tts_config_id = NULL WHERE id = $1", tenant["id"],
    )
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM carriers WHERE tenant_id = $1", tenant["id"])
    # Soft-deleted users (audit_log actors) still reference the tenant; detach them.
    await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture
async def scoped(test_tenant):
    """Sets the RLS tenant scope for tests calling services directly; reset after so it can't
    leak across tests sharing the session event loop."""
    set_target_tenant(str(test_tenant["id"]))
    yield
    set_target_tenant(None)


@pytest_asyncio.fixture(loop_scope="session")
async def test_superadmin(pool):
    """A real superadmin user and signed token, created per test."""
    email = f"test-superadmin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(email=email, password="test-password-not-real", role="superadmin")
    token = auth.create_access_token(user)
    yield {"user": user, "token": token}
    # Soft-delete: audit_log rows reference this user as actor.
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_admin(pool, test_tenant):
    """Tenant-scoped admin; soft-deleted at teardown since it can be an audit_log actor."""
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="admin", tenant_id=test_tenant["id"],
    )
    token = auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_viewer(pool, test_tenant):
    """Read-only user in test_tenant; hard-deleted since it never becomes an audit actor."""
    email = f"test-viewer-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="viewer", tenant_id=test_tenant["id"],
    )
    token = auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("DELETE FROM users WHERE id = $1", user["id"])
