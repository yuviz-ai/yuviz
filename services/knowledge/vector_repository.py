"""
IVectorRepository — the one abstraction retrieval.py depends on for
similarity search, so swapping pgvector for a dedicated vector store later
(Qdrant, pgvector-on-a-different-node, ...) touches one class, never
retrieval.py's ranking/policy logic.

PgVectorRepository is the only implementation today: kb_chunks.embedding
(vector(768), HNSW index, cosine ops — see database/knowledge_schema.sql)
in the same Postgres database everything else in this service uses. The
`<=>` operator is pgvector's cosine *distance*; similarity = 1 - distance,
computed here so callers only ever see "higher is more relevant".

search() only ever considers kb_documents.usage_mode = 'auto' — a
'prompt'-mode document (always injected regardless of relevance; see
kb_documents.usage_mode) is fetched by retrieval.py's separate
always-include path instead, never by similarity search. Without this
filter a manually-flipped-to-prompt document that already has real
embeddings would show up in both paths and get double-counted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg


@dataclass(frozen=True)
class VectorMatch:
    chunk_id: str
    document_id: str
    kb_id: str
    content: str
    score: float  # cosine similarity, higher = more relevant
    token_count: int | None
    page: int | None
    language: str | None
    tags: dict[str, Any]
    version: int
    document_title: str
    kb_slug: str


class IVectorRepository(Protocol):
    async def search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        embedding: list[float],
        top_k: int,
        minimum_score: float,
    ) -> list[VectorMatch]: ...


class PgVectorRepository:
    """Stateless: the connection is opened by the caller (routers/retrieve.py,
    under tenant_conn()/platform_conn()) and passed to search() rather than
    held here, so this singleton never runs a query outside the request's
    own resolved GUC scope (RLS design, libs/tenancy)."""

    async def search(
        self,
        conn: asyncpg.Connection,
        kb_ids: list[str],
        embedding: list[float],
        top_k: int,
        minimum_score: float,
    ) -> list[VectorMatch]:
        if not kb_ids:
            return []
        vector_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
        rows = await conn.fetch(
            """
            SELECT c.id AS chunk_id, c.document_id, c.kb_id, c.content, c.token_count,
                   c.page, c.language, c.tags, c.version,
                   d.title AS document_title, kb.slug AS kb_slug,
                   1 - (c.embedding <=> $1::vector) AS score
            FROM kb_chunks c
            JOIN kb_documents d ON d.id = c.document_id AND d.deleted_at IS NULL AND d.usage_mode = 'auto'
            JOIN knowledge_bases kb ON kb.id = c.kb_id
            WHERE c.kb_id = ANY($2::uuid[]) AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> $1::vector
            LIMIT $3
            """,
            vector_literal, kb_ids, top_k,
        )
        matches = [
            VectorMatch(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                kb_id=str(row["kb_id"]),
                content=row["content"],
                score=row["score"],
                token_count=row["token_count"],
                page=row["page"],
                language=row["language"],
                tags=json.loads(row["tags"]) if row["tags"] else {},
                version=row["version"],
                document_title=row["document_title"],
                kb_slug=row["kb_slug"],
            )
            for row in rows
        ]
        return [m for m in matches if m.score >= minimum_score]
