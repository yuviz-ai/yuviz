"""
Full pipeline test: upload (text/plain) -> process_one_job() (chunk, embed
via real Ollama, insert kb_chunks) -> document/job land in their terminal
states. Real Ollama + real Postgres, matching this project's testing
convention for infra that's fast/available locally.
"""

from __future__ import annotations

from libs.tenancy import set_caller_tenant
from services.config import provider_configs
from services.knowledge import documents as documents_service
from services.knowledge import knowledge_bases as kb_service
from services.knowledge.embedding_manager import EmbeddingProviderManager
from services.knowledge.ingestion_worker import process_one_job
from services.knowledge.secret_resolver import CompositeSecretResolver
from services.knowledge.storage import LocalStorageProvider


async def test_process_one_job_success_produces_ready_document_and_chunks(tenant_agent, pool):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="policies", name="Policies", embedding_config_id=embedding_cfg["id"],
    )
    # Content well over AUTO_INLINE_THRESHOLD_BYTES (500) so this exercises
    # the normal chunk+embed path, not the auto-inline one — see
    # test_tiny_document_auto_inlines_and_skips_embedding below for that.
    content = (
        b"Refunds are processed within 30 days of the original purchase date, "
        b"provided the item is returned in its original packaging with proof "
        b"of purchase. Shipping typically takes 3 to 5 business days for "
        b"domestic orders and 7 to 14 business days for international orders. "
        b"Expedited shipping options are available at checkout for an additional fee. "
        b"Customers may also request store credit in lieu of a refund, which is "
        b"issued immediately upon receipt of the returned item at our warehouse. "
        b"For further questions, contact our support team via the help center."
    )
    assert len(content) >= 500
    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="Refund Policy", filename="refund.txt",
        content_type="text/plain", content=content, storage=LocalStorageProvider(),
    )
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))

    manager = EmbeddingProviderManager(CompositeSecretResolver())
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    updated_doc = await documents_service.get_document(doc["id"])
    assert updated_doc["status"] == "ready"
    assert updated_doc["error"] is None
    assert updated_doc["usage_mode"] == "auto"

    updated_job = await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", job["id"])
    assert updated_job["status"] == "succeeded"

    chunks = await pool.fetch("SELECT * FROM kb_chunks WHERE document_id = $1 ORDER BY chunk_index", doc["id"])
    assert len(chunks) >= 1
    assert chunks[0]["embedding"] is not None


async def test_tiny_document_auto_inlines_and_skips_embedding(tenant_agent, pool):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="tiny-kb", name="Tiny KB", embedding_config_id=embedding_cfg["id"],
    )
    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="Short Note", filename="note.txt",
        content_type="text/plain", content=b"We are closed on Sundays.", storage=LocalStorageProvider(),
    )
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))

    manager = EmbeddingProviderManager(CompositeSecretResolver())
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    updated_doc = await documents_service.get_document(doc["id"])
    assert updated_doc["status"] == "ready"
    assert updated_doc["usage_mode"] == "prompt"

    chunks = await pool.fetch("SELECT * FROM kb_chunks WHERE document_id = $1", doc["id"])
    assert len(chunks) == 1
    assert chunks[0]["embedding"] is None
    assert chunks[0]["content"] == "We are closed on Sundays."


async def test_manual_prompt_override_survives_reingestion_of_large_document(tenant_agent, pool):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="override-kb", name="Override KB", embedding_config_id=embedding_cfg["id"],
    )
    content = b"x " * 300  # comfortably over the 500-byte auto-inline threshold
    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="Big Doc", filename="big.txt",
        content_type="text/plain", content=content, storage=LocalStorageProvider(),
    )
    await documents_service.update_document(doc["id"], usage_mode="prompt")
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))

    manager = EmbeddingProviderManager(CompositeSecretResolver())
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    updated_doc = await documents_service.get_document(doc["id"])
    assert updated_doc["usage_mode"] == "prompt"  # not silently reset to 'auto' by re-ingestion
    chunks = await pool.fetch("SELECT * FROM kb_chunks WHERE document_id = $1", doc["id"])
    assert chunks[0]["embedding"] is not None  # still embedded normally — only skipped for tiny docs


async def test_process_one_job_unsupported_content_type_marks_failed(tenant_agent, pool):
    tenant, _ = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
    )
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="policies2", name="Policies2", embedding_config_id=embedding_cfg["id"],
    )
    doc = await documents_service.upload_document(
        tenant_id=tenant["id"], kb_id=kb["id"], title="Scan", filename="scan.pdf",
        content_type="application/pdf", content=b"%PDF-1.4 fake",
        storage=LocalStorageProvider(),
    )
    job = dict(await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", doc["ingestion_job_id"]))

    manager = EmbeddingProviderManager(CompositeSecretResolver())
    await process_one_job(pool, LocalStorageProvider(), manager, job)

    updated_doc = await documents_service.get_document(doc["id"])
    assert updated_doc["status"] == "failed"
    assert "unsupported content_type" in updated_doc["error"]

    updated_job = await pool.fetchrow("SELECT * FROM kb_ingestion_jobs WHERE id = $1", job["id"])
    assert updated_job["status"] == "failed"


# ── job claim holds no transaction between poll ticks (T50) ─────────────

async def test_claim_holds_no_transaction_between_poll_ticks(pool):
    from services.knowledge.ingestion_worker import _claim_next_job

    for _ in range(3):
        await _claim_next_job(pool)
        idle_in_txn = await pool.fetch(
            "SELECT pid, query FROM pg_stat_activity WHERE state = 'idle in transaction'",
        )
        assert idle_in_txn == []
