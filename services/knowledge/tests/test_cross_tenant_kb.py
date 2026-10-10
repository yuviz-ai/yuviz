"""A KB can never be attached to, read through, or uploaded into across a tenant boundary.

Two real tenants throughout: `tenant_agent` (the caller, with an admin) and `other_tenant_admin`
(the owner of the foreign KB, document and agent).
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_caller_tenant
from services.config import auth as config_auth
from services.config import users as users_service
from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import retrieval
from services.knowledge.app import app
from services.knowledge.routers import documents as documents_router

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def caller(pool, tenant_agent):
    """Admin of the caller's tenant, with the tenant's agent and a KB of its own."""
    tenant, agent = tenant_agent
    user = await users_service.create_user(
        email=f"test-admin-{uuid.uuid4().hex[:8]}@example.com", password="test-password-not-real",
        role="admin", tenant_id=tenant["id"],
    )
    own_kb = await pool.fetchrow(
        "INSERT INTO knowledge_bases (tenant_id, slug, name) VALUES ($1, 'own', 'Own') RETURNING *", tenant["id"],
    )
    yield {
        "tenant": tenant, "agent": agent, "own_kb": dict(own_kb),
        "headers": {"Authorization": f"Bearer {config_auth.create_access_token(user)}"},
    }
    await pool.execute("UPDATE users SET deleted_at = now(), tenant_id = NULL WHERE id = $1", user["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def foreign(pool, other_tenant_admin):
    """A KB, a document in it and an agent, all owned by the other tenant."""
    tenant = other_tenant_admin["tenant"]
    embedding_config = await pool.fetchrow(
        "INSERT INTO provider_configs (tenant_id, name, role, engine) "
        "VALUES ($1, 'Embed', 'embedding', 'ollama') RETURNING id", tenant["id"],
    )
    kb = dict(await pool.fetchrow(
        "INSERT INTO knowledge_bases (tenant_id, slug, name, embedding_config_id) "
        "VALUES ($1, 'theirs', 'Theirs', $2) RETURNING *", tenant["id"], embedding_config["id"],
    ))
    document = dict(await pool.fetchrow(
        "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type) "
        "VALUES ($1, $2, 'Secret', 'ref', 'text/plain') RETURNING *", kb["id"], tenant["id"],
    ))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'theirs', 'Theirs') RETURNING *", tenant["id"],
    ))
    yield {"kb": kb, "document": document, "agent": agent, "headers": {
        "Authorization": f"Bearer {other_tenant_admin['token']}"}}
    await pool.execute("DELETE FROM agent_knowledge_bases WHERE agent_id = $1 OR kb_id = $2", agent["id"], kb["id"])
    await pool.execute("DELETE FROM kb_ingestion_jobs WHERE document_id = $1", document["id"])
    await pool.execute("DELETE FROM kb_documents WHERE id = $1", document["id"])
    await pool.execute("DELETE FROM knowledge_bases WHERE id = $1", kb["id"])
    await pool.execute("DELETE FROM provider_configs WHERE id = $1", embedding_config["id"])
    await pool.execute("DELETE FROM agents WHERE id = $1", agent["id"])


async def _insert_cross_tenant_row(pool, agent_id, kb_id):
    """Plants the row the trigger forbids, as a pre-fix database could hold it."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("ALTER TABLE agent_knowledge_bases DISABLE TRIGGER agent_knowledge_bases_same_tenant")
        await conn.execute("INSERT INTO agent_knowledge_bases (agent_id, kb_id) VALUES ($1, $2)", agent_id, kb_id)
        await conn.execute("ALTER TABLE agent_knowledge_bases ENABLE TRIGGER agent_knowledge_bases_same_tenant")


async def _attached(pool, agent_id) -> list:
    return await pool.fetch("SELECT * FROM agent_knowledge_bases WHERE agent_id = $1", agent_id)


# ── (1a) attach route ───────────────────────────────────────────────────────────

async def test_attach_own_kb_succeeds(client, pool, caller):
    resp = await client.post(
        f"/agents/{caller['agent']['id']}/knowledge-bases",
        json={"kb_id": str(caller["own_kb"]["id"])}, headers=caller["headers"],
    )
    assert resp.status_code == 201
    assert len(await _attached(pool, caller["agent"]["id"])) == 1


async def test_attach_foreign_kb_is_refused_like_a_nonexistent_kb(client, pool, caller, foreign):
    url = f"/agents/{caller['agent']['id']}/knowledge-bases"
    foreign_resp = await client.post(url, json={"kb_id": str(foreign["kb"]["id"])}, headers=caller["headers"])
    unknown_resp = await client.post(url, json={"kb_id": UNKNOWN_ID}, headers=caller["headers"])

    assert unknown_resp.status_code == 404
    assert foreign_resp.status_code == unknown_resp.status_code
    assert foreign_resp.json()["detail"] == unknown_resp.json()["detail"].replace(UNKNOWN_ID, str(foreign["kb"]["id"]))
    assert await _attached(pool, caller["agent"]["id"]) == []


async def test_attach_to_foreign_agent_is_refused_like_an_unknown_agent(client, pool, caller, foreign):
    foreign_resp = await client.post(
        f"/agents/{foreign['agent']['id']}/knowledge-bases",
        json={"kb_id": str(caller["own_kb"]["id"])}, headers=caller["headers"],
    )
    unknown_resp = await client.post(
        f"/agents/{UNKNOWN_ID}/knowledge-bases",
        json={"kb_id": str(caller["own_kb"]["id"])}, headers=caller["headers"],
    )
    assert unknown_resp.status_code == 404
    assert foreign_resp.status_code == unknown_resp.status_code
    assert foreign_resp.json()["detail"] == unknown_resp.json()["detail"].replace(UNKNOWN_ID, str(foreign["agent"]["id"]))
    assert await _attached(pool, foreign["agent"]["id"]) == []


async def test_attach_malformed_kb_id_is_the_same_404(client, caller):
    resp = await client.post(
        f"/agents/{caller['agent']['id']}/knowledge-bases", json={"kb_id": "not-a-uuid"}, headers=caller["headers"],
    )
    assert resp.status_code == 404


# ── (1c) the row cannot exist ───────────────────────────────────────────────────

async def test_database_refuses_a_cross_tenant_attachment(pool, caller, foreign):
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO agent_knowledge_bases (agent_id, kb_id) VALUES ($1, $2)",
            caller["agent"]["id"], foreign["kb"]["id"],
        )


async def test_database_refuses_repointing_an_attachment_across_tenants(pool, caller, foreign):
    await pool.execute(
        "INSERT INTO agent_knowledge_bases (agent_id, kb_id) VALUES ($1, $2)",
        caller["agent"]["id"], caller["own_kb"]["id"],
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "UPDATE agent_knowledge_bases SET kb_id = $2 WHERE agent_id = $1",
            caller["agent"]["id"], foreign["kb"]["id"],
        )


# ── (1b, 1d) a stray cross-tenant row is inert for every reader ──────────────────

async def test_a_stray_cross_tenant_row_is_invisible_to_retrieval_and_listing(pool, caller, foreign):
    tenant, agent = caller["tenant"], caller["agent"]
    await _insert_cross_tenant_row(pool, agent["id"], foreign["kb"]["id"])
    set_caller_tenant(str(tenant["id"]))

    async with pool.acquire() as conn:
        assert await retrieval._agent_enabled_kb_ids(conn, tenant["slug"], agent["slug"]) == []
        assert await retrieval._agent_kb_groups(conn, tenant["slug"], agent["slug"]) == {}
    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is False
    assert await agent_kb_service.list_for_agent(agent["id"]) == []
    assert await agent_kb_service.list_for_kb(foreign["kb"]["id"]) == []


# ── (4) foreign is identical to unknown on the by-id routes ─────────────────────

@pytest.mark.parametrize("method, path, foreign_key, kind", [
    ("get", "/agents/{id}/knowledge-bases", "agent", "agent"),
    ("get", "/knowledge-bases/{id}/documents", "kb", "knowledge_base"),
    ("get", "/documents/{id}", "document", "kb_document"),
    ("delete", "/documents/{id}", "document", "kb_document"),
    ("post", "/documents/{id}/retry", "document", "kb_document"),
])
async def test_foreign_id_is_indistinguishable_from_an_unknown_id(
    client, caller, foreign, method, path, foreign_key, kind,
):
    foreign_id = str(foreign[foreign_key]["id"])
    foreign_resp = await getattr(client, method)(path.format(id=foreign_id), headers=caller["headers"])
    unknown_resp = await getattr(client, method)(path.format(id=UNKNOWN_ID), headers=caller["headers"])

    assert unknown_resp.status_code == 404
    assert foreign_resp.status_code == unknown_resp.status_code
    assert foreign_resp.json()["detail"] == unknown_resp.json()["detail"].replace(UNKNOWN_ID, foreign_id)


async def test_foreign_kb_upload_is_refused_like_an_unknown_kb(client, caller, foreign):
    files = {"file": ("a.txt", b"hello", "text/plain")}
    foreign_resp = await client.post(
        f"/knowledge-bases/{foreign['kb']['id']}/documents", files=files, data={"title": "t"}, headers=caller["headers"],
    )
    unknown_resp = await client.post(
        f"/knowledge-bases/{UNKNOWN_ID}/documents", files=files, data={"title": "t"}, headers=caller["headers"],
    )
    assert unknown_resp.status_code == 404
    assert foreign_resp.status_code == unknown_resp.status_code


# ── (3) upload limits ───────────────────────────────────────────────────────────

def _upload(client, caller, content: bytes, content_type: str):
    return client.post(
        f"/knowledge-bases/{caller['own_kb']['id']}/documents",
        files={"file": ("a.txt", content, content_type)}, data={"title": "t"}, headers=caller["headers"],
    )


@pytest_asyncio.fixture(loop_scope="session")
async def doc_cleanup(pool, caller):
    yield
    await pool.execute(
        "DELETE FROM kb_ingestion_jobs WHERE kb_id = $1", caller["own_kb"]["id"],
    )
    await pool.execute("DELETE FROM kb_documents WHERE kb_id = $1", caller["own_kb"]["id"])


async def test_upload_within_the_limit_is_accepted(client, caller, doc_cleanup, monkeypatch):
    monkeypatch.setattr(documents_router, "MAX_UPLOAD_BYTES", 100)
    assert (await _upload(client, caller, b"x" * 100, "text/plain")).status_code == 201


async def test_upload_over_the_limit_is_413(client, caller, doc_cleanup, monkeypatch):
    monkeypatch.setattr(documents_router, "MAX_UPLOAD_BYTES", 100)
    assert (await _upload(client, caller, b"x" * 101, "text/plain")).status_code == 413


async def test_oversize_upload_stops_reading_early(caller, monkeypatch):
    monkeypatch.setattr(documents_router, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(documents_router, "_READ_CHUNK_BYTES", 10)

    class Counting:
        reads = 0

        async def read(self, size):
            Counting.reads += 1
            return b"x" * size if Counting.reads <= 50 else b""

    with pytest.raises(Exception) as exc:
        await documents_router._read_capped(Counting())
    assert exc.value.status_code == 413
    assert Counting.reads == 11


async def test_retry_requeues_a_failed_document_once(client, pool, caller, doc_cleanup):
    doc = (await _upload(client, caller, b"hello", "text/plain")).json()
    await pool.execute("UPDATE kb_documents SET status = 'failed', error = 'boom' WHERE id = $1", doc["id"])

    first = await client.post(f"/documents/{doc['id']}/retry", headers=caller["headers"])
    second = await client.post(f"/documents/{doc['id']}/retry", headers=caller["headers"])

    assert first.status_code == 200
    assert first.json()["status"] == "pending" and first.json()["error"] is None
    assert second.status_code == 409
    jobs = await pool.fetch("SELECT status FROM kb_ingestion_jobs WHERE document_id = $1", doc["id"])
    assert [j["status"] for j in jobs] == ["pending", "pending"]


async def test_retry_is_admin_only(client, pool, caller, test_viewer, doc_cleanup):
    doc = (await _upload(client, caller, b"hello", "text/plain")).json()
    await pool.execute("UPDATE kb_documents SET status = 'failed' WHERE id = $1", doc["id"])
    resp = await client.post(
        f"/documents/{doc['id']}/retry", headers={"Authorization": f"Bearer {test_viewer['token']}"},
    )
    assert resp.status_code == 403
    assert await pool.fetchval("SELECT status FROM kb_documents WHERE id = $1", doc["id"]) == "failed"


@pytest.mark.parametrize("content_type, status", [
    ("text/plain", 201),
    ("text/markdown", 201),
    ("Text/Plain; charset=utf-8", 201),
    ("application/pdf", 415),
    ("application/octet-stream", 415),
])
async def test_upload_content_type_is_enforced(client, caller, doc_cleanup, content_type, status):
    assert (await _upload(client, caller, b"hello", content_type)).status_code == status
