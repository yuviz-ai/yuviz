from __future__ import annotations

import pytest

from libs.tenancy import set_caller_tenant
from services.knowledge import documents as documents_service
from services.knowledge import knowledge_bases as kb_service
from services.knowledge.storage import LocalStorageProvider


async def _make_kb(tenant):
    set_caller_tenant(str(tenant["id"]))
    return await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug="policies", name="Policies")


async def test_upload_creates_document_and_ingestion_job(tenant_agent, pool):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant)
    storage = LocalStorageProvider()

    result = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="Refund Policy", filename="refund.txt",
        content_type="text/plain", content=b"Refunds take 30 days.", storage=storage,
    )

    assert result["status"] == "pending"
    assert result["source_ref"]
    assert (await storage.read(result["source_ref"])) == b"Refunds take 30 days."

    job = await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", result["ingestion_job_id"])
    assert job is not None and job["status"] == "pending" and job["document_id"] == result["id"]


async def test_update_document_rejects_unknown_field(tenant_agent):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant)
    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="T", filename="f.txt",
        content_type="text/plain", content=b"x", storage=LocalStorageProvider(),
    )
    with pytest.raises(ValueError):
        await documents_service.update_document(doc["id"], status="ready")


async def test_soft_delete_excludes_from_get_and_list(tenant_agent):
    tenant, _ = tenant_agent
    kb = await _make_kb(tenant)
    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="T", filename="f.txt",
        content_type="text/plain", content=b"x", storage=LocalStorageProvider(),
    )

    await documents_service.soft_delete_document(doc["id"])

    assert await documents_service.get_document(doc["id"]) is None
    assert doc["id"] not in {d["id"] for d in await documents_service.list_documents(kb["id"])}
