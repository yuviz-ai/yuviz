from __future__ import annotations

import os
import uuid

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)

import pytest_asyncio

from services.config import auth as config_auth
from services.config import users as users_service
from services.did import db  # noqa: E402


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await db.get_pool()
    yield p


@pytest_asyncio.fixture(loop_scope="session")
async def test_tenant(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Test Tenant {slug}", slug,
    )
    tenant = dict(row)
    yield tenant
    await pool.execute("DELETE FROM purchased_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM carriers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_carrier(pool, test_tenant):
    row = await pool.fetchrow(
        "INSERT INTO carriers (tenant_id, name, provider, auth_id, auth_token_ref) "
        "VALUES ($1, 'Test Plivo', 'plivo', 'MAtest', 'env:PLIVO_AUTH_TOKEN') RETURNING *",
        test_tenant["id"],
    )
    return dict(row)


@pytest_asyncio.fixture(loop_scope="session")
async def test_superadmin(pool):
    email = f"test-superadmin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(email=email, password="test-password-not-real", role="superadmin")
    token = config_auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def foreign_tenant_admin(pool):
    """A tenant admin scoped to a tenant OTHER than `test_tenant` — the
    actor design Q6 names as the DID purchase hole: `require_role`-only
    routes let any tenant admin purchase into another tenant's path."""
    slug = f"test-foreign-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Foreign Tenant {slug}", slug,
    ))
    email = f"test-foreign-admin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="admin", tenant_id=tenant["id"],
    )
    token = config_auth.create_access_token(user)
    yield {"user": user, "token": token, "tenant": tenant}
    await pool.execute("DELETE FROM users WHERE id = $1", user["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
