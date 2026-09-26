from __future__ import annotations

import os
import uuid

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)

import pytest_asyncio

from libs.tenancy import set_target_tenant  # noqa: E402
from services.campaigns import db  # noqa: E402


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
    await pool.execute(
        "DELETE FROM campaign_contacts WHERE campaign_id IN (SELECT id FROM campaigns WHERE tenant_id = $1)",
        tenant["id"],
    )
    await pool.execute("DELETE FROM campaigns WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM dnc_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_agent(pool, test_tenant):
    row = await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'test-agent', 'Test Agent') RETURNING *",
        test_tenant["id"],
    )
    return dict(row)


@pytest_asyncio.fixture
async def scoped(test_tenant):
    """Sets the ambient RLS tenant scope for tests that call services/campaigns
    functions directly, bypassing the HTTP layer — without this, tenant_conn()
    raises TenantUnresolved. Mirrors services/config/tests/conftest.py's fixture
    of the same name."""
    set_target_tenant(str(test_tenant["id"]))
    yield
    set_target_tenant(None)
