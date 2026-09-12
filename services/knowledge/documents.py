"""
kb_documents CRUD + upload flow. upload_document() is the one function that
ties storage (StorageProvider), the document row, and the ingestion queue
(kb_ingestion_jobs) together in one call — the three things that must never
go out of sync: a document row with no storage-saved bytes, or bytes saved
with no job queued to chunk/embed them.
"""

from __future__ import annotations

import json
from typing import Any

from . import audit, db
from .storage import StorageProvider

_UPDATABLE_FIELDS = {"title", "language", "tags", "usage_mode"}


async def get_document(document_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM kb_documents WHERE id = $1 AND deleted_at IS NULL", document_id,
    )
    return dict(row) if row is not None else None


async def list_documents(kb_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    # chunk_count is scoped to chunks matching the document's *current*
    # version — an older version's chunks are leftover history from a
    # previous ingestion run, not what's live in retrieval today.
    rows = await pool.fetch(
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM kb_chunks c WHERE c.document_id = d.id AND c.version = d.version) AS chunk_count "
        "FROM kb_documents d WHERE d.kb_id = $1 AND d.deleted_at IS NULL ORDER BY d.created_at DESC",
        kb_id,
    )
    return [dict(row) for row in rows]


async def upload_document(
    *,
    tenant_id: Any,
    kb_id: Any,
    title: str,
    filename: str,
    content_type: str,
    content: bytes,
    storage: StorageProvider,
    language: str | None = None,
    tags: dict[str, Any] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, language, tags) "
                "VALUES ($1, $2, $3, '', $4, $5, $6::jsonb) RETURNING *",
                kb_id, tenant_id, title, content_type, language, json.dumps(tags or {}),
            )
            document = dict(row)

            # Bytes are saved before source_ref is committed, and source_ref
            # is filled in the same transaction as the insert it belongs to
            # — a crash between save() and this UPDATE leaves an orphaned
            # file (acceptable: garbage-collectable, never a document row
            # pointing at nothing), never a document row with a real
            # source_ref that doesn't exist.
            source_ref = await storage.save(str(tenant_id), str(kb_id), filename, content)
            row = await conn.fetchrow(
                "UPDATE kb_documents SET source_ref = $2 WHERE id = $1 RETURNING *", document["id"], source_ref,
            )
            document = dict(row)

            job_row = await conn.fetchrow(
                "INSERT INTO kb_ingestion_jobs (document_id, kb_id) VALUES ($1, $2) RETURNING *",
                document["id"], kb_id,
            )

            await audit.write_audit(
                conn, entity_type="kb_document", entity_id=document["id"], action="created",
                user_id=user_id, user_email=user_email, new_value=document,
            )
    return {**document, "ingestion_job_id": job_row["id"]}


async def update_document(
    document_id: Any, *, user_id: Any | None = None, user_email: str | None = None, **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_document() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_document() got non-updatable field(s): {unknown}")

    if "tags" in fields:
        fields = {**fields, "tags": json.dumps(fields["tags"])}

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow("SELECT * FROM kb_documents WHERE id = $1 FOR UPDATE", document_id)
            if old_row is None:
                raise LookupError(f"kb_document {document_id} not found")
            old = dict(old_row)

            columns = list(fields.keys())
            set_parts = []
            for i, col in enumerate(columns):
                cast = "::jsonb" if col == "tags" else ""
                set_parts.append(f"{col} = ${i + 2}{cast}")
            new_row = await conn.fetchrow(
                f"UPDATE kb_documents SET {', '.join(set_parts)}, updated_at = now() WHERE id = $1 RETURNING *",
                document_id, *(fields[col] for col in columns),
            )
            new = dict(new_row)

            await audit.write_audit(
                conn, entity_type="kb_document", entity_id=document_id, action="updated",
                user_id=user_id, user_email=user_email, old_value=old, new_value=new,
            )
    return new


async def soft_delete_document(
    document_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow("SELECT * FROM kb_documents WHERE id = $1 FOR UPDATE", document_id)
            if old_row is None:
                raise LookupError(f"kb_document {document_id} not found")
            old = dict(old_row)

            await conn.execute("UPDATE kb_documents SET deleted_at = now() WHERE id = $1", document_id)
            await audit.write_audit(
                conn, entity_type="kb_document", entity_id=document_id, action="deleted",
                user_id=user_id, user_email=user_email, old_value=old,
            )
