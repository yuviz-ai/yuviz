from __future__ import annotations

import os
import uuid

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)
os.environ.setdefault("KNOWLEDGE_STORAGE_ROOT", "/tmp/voiceai-knowledge-test-storage")

import pytest_asyncio

from services.config import auth as config_auth  # noqa: E402
from services.config import users as users_service  # noqa: E402
from services.knowledge import db  # noqa: E402


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
        "DELETE FROM agent_knowledge_bases WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)",
        tenant["id"],
    )
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_admin(pool, test_tenant):
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="admin", tenant_id=test_tenant["id"],
    )
    token = config_auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_viewer(pool, test_tenant):
    email = f"test-viewer-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="viewer", tenant_id=test_tenant["id"],
    )
    token = config_auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_service_viewer(pool):
    """NULL-tenant viewer service account (lesson 24) — the shape Conversation's
    startup prewarm and vobiz's per-call telephony lookup authenticate as."""
    email = f"test-service-viewer-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(email=email, password="test-password-not-real", role="viewer")
    token = config_auth.create_access_token(user)
    yield {"user": user, "token": token}
    await pool.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def other_tenant_admin(pool):
    """A second tenant + its admin, for asserting the caller-tenant predicate
    against a KB that genuinely belongs to a *different* tenant than the
    caller (lesson 12 — the query's own joins can't reach this case)."""
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Test Tenant {slug}", slug,
    ))
    email = f"test-admin-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role="admin", tenant_id=tenant["id"],
    )
    token = config_auth.create_access_token(user)
    yield {"user": user, "token": token, "tenant": tenant}
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])
    await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_agent(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Test {slug}", slug,
    ))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"],
    ))
    yield tenant, agent
    await pool.execute("DELETE FROM agent_retrieval_policies WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", tenant["id"])
    await pool.execute(
        "DELETE FROM kb_ingestion_jobs WHERE document_id IN "
        "(SELECT id FROM kb_documents WHERE tenant_id = $1)", tenant["id"],
    )
    await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
