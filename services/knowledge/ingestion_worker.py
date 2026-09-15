"""
Background ingestion worker — separate process (see __main__.py), polling
kb_ingestion_jobs the same way services/config/phone_numbers.py's
prewarm() precedent established for this codebase: a Postgres-backed job
table + a polling loop, no Celery/RQ/broker introduced for one worker.

Pipeline per job: read bytes (StorageProvider) -> extract text -> chunk
(chunking.py) -> embed (EmbeddingProviderManager) -> insert kb_chunks ->
mark document 'ready' and job 'succeeded'. Any failure marks both 'failed'
with the error recorded, and the document stays visible (not deleted) so an
admin can see why and retry.

Supported content types today: text/plain, text/markdown — the two this
project can extract text from with zero new dependencies. Anything else
fails fast with a clear, actionable error rather than silently producing
zero chunks.

Documents under AUTO_INLINE_THRESHOLD_BYTES skip embedding entirely and
are marked usage_mode='prompt' (see kb_documents.usage_mode in
database/knowledge_schema.sql) — embedding + vector search adds ~250ms of
latency for a document this small with no retrieval-quality benefit, the
same threshold and reasoning ElevenLabs' own RAG implementation documents.
Still stored as a single kb_chunks row with embedding=NULL, so retrieval.
py's always-include-in-prompt path (which reads kb_chunks uniformly
regardless of how a document got its usage_mode) needs no special case
for a document that was never actually chunked.

Job claim is cross-tenant by construction (the poll query and this job's
own status flip run before any tenant is known), so both go through
platform_conn(); once doc_row is read and its tenant_id known, every
further statement re-scopes to that tenant via
tenant_conn(explicit_tenant=...) — a second, independent check that the
claimed job body actually belongs to the tenant it claims to (RLS design,
libs/tenancy).
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from libs.tenancy import platform_conn, tenant_conn

from . import db
from .chunking import Chunk, chunk_text
from .embedding_manager import EmbeddingProviderConfig, EmbeddingProviderManager
from .storage import StorageProvider

log = logging.getLogger(__name__)

_SUPPORTED_CONTENT_TYPES = {"text/plain", "text/markdown"}
AUTO_INLINE_THRESHOLD_BYTES = 500
POLL_INTERVAL_S = 2.0


async def _fetch_embedding_config(conn: asyncpg.Connection, kb_id) -> EmbeddingProviderConfig:
    row = await conn.fetchrow(
        "SELECT pc.* FROM knowledge_bases kb "
        "JOIN provider_configs pc ON pc.id = kb.embedding_config_id "
        "WHERE kb.id = $1 AND pc.deleted_at IS NULL",
        kb_id,
    )
    if row is None:
        raise ValueError(f"knowledge_base {kb_id} has no embedding_config_id configured")
    extra = json.loads(row["extra"]) if row["extra"] else {}
    return EmbeddingProviderConfig(
        id=str(row["id"]), engine=row["engine"], model=row["model"],
        api_key_ref=row["api_key_ref"], extra=extra,
    )


async def _extract_text(content_type: str, raw: bytes) -> str:
    if content_type not in _SUPPORTED_CONTENT_TYPES:
        raise ValueError(
            f"unsupported content_type={content_type!r} — supported: {sorted(_SUPPORTED_CONTENT_TYPES)}",
        )
    return raw.decode("utf-8")


async def process_one_job(
    pool: asyncpg.Pool,
    storage: StorageProvider,
    embedding_manager: EmbeddingProviderManager,
    job: dict,
) -> None:
    document_id, kb_id = job["document_id"], job["kb_id"]

    async with platform_conn(pool, reason="kb-ingestion-job-claim") as conn:
        await conn.execute(
            "UPDATE kb_ingestion_jobs SET status = 'running', started_at = now(), attempts = attempts + 1 "
            "WHERE id = $1",
            job["id"],
        )
        await conn.execute("UPDATE kb_documents SET status = 'processing' WHERE id = $1", document_id)
        doc_row = await conn.fetchrow("SELECT * FROM kb_documents WHERE id = $1", document_id)

    if doc_row is None:
        raise ValueError(f"kb_document {document_id} not found")
    tenant_id = str(doc_row["tenant_id"])

    try:
        raw = await storage.read(doc_row["source_ref"])
        text = await _extract_text(doc_row["content_type"], raw)

        if len(raw) < AUTO_INLINE_THRESHOLD_BYTES:
            new_chunks: list[Chunk] = [Chunk(content=text, token_count=len(text.split()))]
            vectors: list[list[float] | None] = [None]
            new_usage_mode = "prompt"
        else:
            new_chunks = chunk_text(text)
            if not new_chunks:
                raise ValueError("document produced zero chunks (empty or unextractable content)")
            async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
                embedding_cfg = await _fetch_embedding_config(conn, kb_id)
            provider = await embedding_manager.get(embedding_cfg)
            vectors = await provider.embed([c.content for c in new_chunks])
            # Above the auto-inline threshold, usage_mode is whatever it
            # already was — an admin's manual 'prompt' override survives
            # re-ingestion; it's never silently reset to 'auto'.
            new_usage_mode = doc_row["usage_mode"]

        async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
            # New version's chunks are inserted before the old version's
            # are removed, and only for this document_id — a concurrent
            # retrieve() against a different document is unaffected
            # either way.
            new_version = (doc_row["version"] or 1) + 1 if doc_row["status"] == "ready" else doc_row["version"]
            await conn.execute("DELETE FROM kb_chunks WHERE document_id = $1", document_id)
            for i, (chunk, vector) in enumerate(zip(new_chunks, vectors)):
                vector_literal = None if vector is None else "[" + ",".join(repr(float(x)) for x in vector) + "]"
                await conn.execute(
                    "INSERT INTO kb_chunks "
                    "(document_id, kb_id, tenant_id, chunk_index, content, embedding, "
                    " token_count, language, version) "
                    "VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8, $9)",
                    document_id, kb_id, doc_row["tenant_id"], i, chunk.content,
                    vector_literal, chunk.token_count, doc_row["language"], new_version,
                )
            await conn.execute(
                "UPDATE kb_documents SET status = 'ready', error = NULL, version = $2, "
                "usage_mode = $3, updated_at = now() WHERE id = $1",
                document_id, new_version, new_usage_mode,
            )
            await conn.execute(
                "UPDATE kb_ingestion_jobs SET status = 'succeeded', finished_at = now() WHERE id = $1",
                job["id"],
            )
        log.info("Ingested document=%s kb=%s chunks=%d usage_mode=%s", document_id, kb_id, len(new_chunks), new_usage_mode)

    except Exception as exc:
        log.exception("Ingestion failed document=%s kb=%s", document_id, kb_id)
        async with tenant_conn(pool, explicit_tenant=tenant_id, reason="kb-ingestion-job") as conn:
            await conn.execute(
                "UPDATE kb_documents SET status = 'failed', error = $2 WHERE id = $1", document_id, str(exc),
            )
            await conn.execute(
                "UPDATE kb_ingestion_jobs SET status = 'failed', error = $2, finished_at = now() WHERE id = $1",
                job["id"], str(exc),
            )


async def _claim_next_job(pool: asyncpg.Pool) -> dict | None:
    """One poll tick — picks one pending job (FOR UPDATE SKIP LOCKED would
    matter for multiple worker processes; this project runs exactly one
    worker process, so a plain SELECT is sufficient today). Its own
    context manager closes before returning, so no transaction is held
    across the loop's sleep between ticks."""
    async with platform_conn(pool, reason="kb-ingestion-job-claim") as conn:
        job_row = await conn.fetchrow(
            "SELECT * FROM kb_ingestion_jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1",
        )
    return dict(job_row) if job_row is not None else None


async def run_loop(
    storage: StorageProvider,
    embedding_manager: EmbeddingProviderManager,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> None:
    """Runs forever — the ingestion worker's process entry point (see
    __main__.py). The poll stays outside any transaction between
    iterations — a worker loop that held one open would pin a snapshot
    and locks for the loop's lifetime."""
    pool = await db.get_pool()
    while True:
        job = await _claim_next_job(pool)
        if job is None:
            await asyncio.sleep(poll_interval_s)
            continue
        await process_one_job(pool, storage, embedding_manager, job)
