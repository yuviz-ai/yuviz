"""HttpKnowledgeRepository against the real Config (auth) and Knowledge apps in-process."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from httpx import ASGITransport

from libs.knowledge_sdk.exceptions import RepositoryUnavailableError
from libs.knowledge_sdk.models import RetrievalPolicy
from libs.knowledge_sdk.repositories.http_repository import HttpKnowledgeRepository
from libs.tenancy import set_target_tenant
from services.config import agents, provider_configs, tenants, users
from services.config import db as config_db
from services.config.app import app as config_app
from services.knowledge.app import app as knowledge_app


@contextmanager
def _seeding_as(tenant_id):
    """Binds the tenant only while seeding via Config's service functions,
    so the HTTP calls under test resolve their own scope."""
    set_target_tenant(str(tenant_id))
    try:
        yield
    finally:
        set_target_tenant(None)


@pytest.fixture
async def service_account():
    email = f"test-service-{uuid.uuid4().hex[:8]}@example.com"
    user = await users.create_user(email=email, password="service-password", role="viewer")
    # Same as scripts/create_service_account.py.
    pool = await config_db.get_pool()
    await pool.execute("UPDATE users SET is_service_account = true WHERE id = $1", user["id"])
    yield {"email": email, "password": "service-password"}
    from services.config import db
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


def _repo(service_account) -> HttpKnowledgeRepository:
    return HttpKnowledgeRepository(
        base_url="http://knowledge-test",
        auth_base_url="http://config-test",
        service_email=service_account["email"],
        service_password=service_account["password"],
        transport=ASGITransport(app=knowledge_app),
        auth_transport=ASGITransport(app=config_app),
    )


@pytest.fixture
async def tenant_agent(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = await tenants.create_tenant(name="Test Tenant", slug=slug)
    with _seeding_as(tenant["id"]):
        agent = await agents.create_agent(tenant_id=tenant["id"], slug="sup", name="Support", tenant_slug=slug)
    yield tenant, agent
    await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_has_enabled_kb_false_when_nothing_attached(service_account, tenant_agent):
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    assert await repo.has_enabled_kb(tenant["slug"], agent["slug"]) is False
    await repo.close()


async def test_has_enabled_kb_true_after_assignment(service_account, tenant_agent, pool):
    tenant, agent = tenant_agent
    with _seeding_as(tenant["id"]):
        embedding_cfg = await provider_configs.create_provider_config(
            tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
        )
    kb = await pool.fetchrow(
        "INSERT INTO knowledge_bases (tenant_id, slug, name, embedding_config_id) "
        "VALUES ($1, 'policies', 'Policies', $2) RETURNING *",
        tenant["id"], embedding_cfg["id"],
    )
    await pool.execute(
        "INSERT INTO agent_knowledge_bases (agent_id, kb_id, enabled) VALUES ($1, $2, true)",
        agent["id"], kb["id"],
    )

    repo = _repo(service_account)
    assert await repo.has_enabled_kb(tenant["slug"], agent["slug"]) is True
    await repo.close()


async def test_retrieve_returns_none_when_404(service_account, tenant_agent):
    tenant, agent = tenant_agent
    repo = _repo(service_account)
    result = await repo.retrieve(tenant["slug"], agent["slug"], "anything", RetrievalPolicy())
    assert result is None
    await repo.close()


async def test_wrong_service_password_raises_unavailable(pool):
    email = f"test-service-{uuid.uuid4().hex[:8]}@example.com"
    user = await users.create_user(email=email, password="correct-password", role="viewer")

    repo = HttpKnowledgeRepository(
        base_url="http://knowledge-test", auth_base_url="http://config-test",
        service_email=email, service_password="wrong-password",
        transport=ASGITransport(app=knowledge_app), auth_transport=ASGITransport(app=config_app),
    )
    with pytest.raises(RepositoryUnavailableError):
        await repo.has_enabled_kb("acme", "sup")

    await repo.close()
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])
