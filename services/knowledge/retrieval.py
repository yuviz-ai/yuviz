"""retrieve() backs POST /internal/retrieve. Policy precedence: call override > agent row > system default.
KBs are grouped by embedding config (one query embedding per group); usage_mode='prompt' docs are always
included first. Returns None (router → 404, "no context") when nothing qualifies."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .embedding_manager import EmbeddingProviderConfig, EmbeddingProviderManager
from .vector_repository import IVectorRepository, VectorMatch

_SYSTEM_DEFAULT_POLICY: dict[str, Any] = {
    "top_k": 5,
    "max_tokens": 1000,
    "minimum_score": 0.3,
    "rerank": False,
    "hybrid_search": False,
    "include_citations": True,
}


async def _agent_id_for(conn: asyncpg.Connection, tenant_slug: str, agent_slug: str) -> str | None:
    row = await conn.fetchrow(
        "SELECT a.id FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE t.slug = $1 AND a.slug = $2 AND a.deleted_at IS NULL AND t.deleted_at IS NULL",
        tenant_slug, agent_slug,
    )
    return str(row["id"]) if row is not None else None


async def _resolve_policy(
    conn: asyncpg.Connection, tenant_slug: str, agent_slug: str, overrides: dict[str, Any],
) -> dict[str, Any]:
    agent_id = await _agent_id_for(conn, tenant_slug, agent_slug)
    agent_policy_row = None
    if agent_id is not None:
        agent_policy_row = await conn.fetchrow(
            "SELECT * FROM agent_retrieval_policies WHERE agent_id = $1", agent_id,
        )

    resolved: dict[str, Any] = {}
    for field, system_default in _SYSTEM_DEFAULT_POLICY.items():
        override_value = overrides.get(field)
        if override_value is not None:
            resolved[field] = override_value
        elif agent_policy_row is not None and agent_policy_row[field] is not None:
            resolved[field] = agent_policy_row[field]
        else:
            resolved[field] = system_default
    return resolved


async def _fetch_embedding_config(conn: asyncpg.Connection, embedding_config_id: str) -> EmbeddingProviderConfig:
    row = await conn.fetchrow(
        "SELECT * FROM provider_configs WHERE id = $1 AND deleted_at IS NULL", embedding_config_id,
    )
    if row is None:
        raise ValueError(f"provider_config {embedding_config_id} not found")
    extra = json.loads(row["extra"]) if row["extra"] else {}
    return EmbeddingProviderConfig(
        id=str(row["id"]), engine=row["engine"], model=row["model"],
        api_key_ref=row["api_key_ref"], extra=extra,
    )


async def _agent_kb_groups(conn: asyncpg.Connection, tenant_slug: str, agent_slug: str) -> dict[str, list[str]]:
    """{embedding_config_id: [kb_id, ...]} for the agent's enabled, active KBs."""
    rows = await conn.fetch(
        "SELECT kb.id AS kb_id, kb.embedding_config_id "
        "FROM agent_knowledge_bases akb "
        "JOIN agents a ON a.id = akb.agent_id AND a.deleted_at IS NULL "
        "JOIN tenants t ON t.id = a.tenant_id AND t.deleted_at IS NULL "
        "JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.deleted_at IS NULL AND kb.status = 'active' "
        "WHERE t.slug = $1 AND a.slug = $2 AND akb.enabled AND kb.embedding_config_id IS NOT NULL",
        tenant_slug, agent_slug,
    )
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(str(row["embedding_config_id"]), []).append(str(row["kb_id"]))
    return groups


async def _agent_enabled_kb_ids(conn: asyncpg.Connection, tenant_slug: str, agent_slug: str) -> list[str]:
    """Enabled, active KBs, including those with no embedding config (prompt-mode only)."""
    rows = await conn.fetch(
        "SELECT kb.id AS kb_id "
        "FROM agent_knowledge_bases akb "
        "JOIN agents a ON a.id = akb.agent_id AND a.deleted_at IS NULL "
        "JOIN tenants t ON t.id = a.tenant_id AND t.deleted_at IS NULL "
        "JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.deleted_at IS NULL AND kb.status = 'active' "
        "WHERE t.slug = $1 AND a.slug = $2 AND akb.enabled",
        tenant_slug, agent_slug,
    )
    return [str(row["kb_id"]) for row in rows]


async def _fetch_prompt_mode_matches(conn: asyncpg.Connection, kb_ids: list[str]) -> list[VectorMatch]:
    """One VectorMatch per prompt-mode document (chunks reassembled); score=1.0 is an "always include" sentinel."""
    if not kb_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT
            (array_agg(c.id ORDER BY c.chunk_index))[1] AS chunk_id,
            c.document_id, c.kb_id,
            string_agg(c.content, ' ' ORDER BY c.chunk_index) AS content,
            sum(coalesce(c.token_count, 0)) AS token_count,
            (array_agg(c.page ORDER BY c.chunk_index))[1] AS page,
            (array_agg(c.language ORDER BY c.chunk_index))[1] AS language,
            (array_agg(c.tags ORDER BY c.chunk_index))[1] AS tags,
            (array_agg(c.version ORDER BY c.chunk_index))[1] AS version,
            d.title AS document_title, kb.slug AS kb_slug
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id AND d.deleted_at IS NULL
                            AND d.usage_mode = 'prompt' AND d.status = 'ready'
        JOIN knowledge_bases kb ON kb.id = c.kb_id
        WHERE c.kb_id = ANY($1::uuid[])
        GROUP BY c.document_id, c.kb_id, d.title, kb.slug
        """,
        kb_ids,
    )
    return [
        VectorMatch(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            kb_id=str(row["kb_id"]),
            content=row["content"],
            score=1.0,
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


def _approx_token_count(text: str) -> int:
    return len(text.split())


async def retrieve(
    conn: asyncpg.Connection,
    vector_repo: IVectorRepository,
    embedding_manager: EmbeddingProviderManager,
    *,
    tenant_slug: str,
    agent_slug: str,
    query: str,
    top_k: int | None = None,
    max_tokens: int | None = None,
    minimum_score: float | None = None,
    rerank: bool | None = None,
    hybrid_search: bool | None = None,
    include_citations: bool | None = None,
) -> dict[str, Any] | None:
    policy = await _resolve_policy(
        conn, tenant_slug, agent_slug,
        {
            "top_k": top_k, "max_tokens": max_tokens, "minimum_score": minimum_score,
            "rerank": rerank, "hybrid_search": hybrid_search, "include_citations": include_citations,
        },
    )

    kb_ids = await _agent_enabled_kb_ids(conn, tenant_slug, agent_slug)
    if not kb_ids:
        return None

    # Prompt-mode docs bypass top_k, minimum_score and the token budget.
    prompt_matches = await _fetch_prompt_mode_matches(conn, kb_ids)

    groups = await _agent_kb_groups(conn, tenant_slug, agent_slug)
    all_matches: list[VectorMatch] = []
    for embedding_config_id, group_kb_ids in groups.items():
        embedding_cfg = await _fetch_embedding_config(conn, embedding_config_id)
        provider = await embedding_manager.get(embedding_cfg)
        [query_vector] = await provider.embed([query])
        matches = await vector_repo.search(conn, group_kb_ids, query_vector, policy["top_k"], policy["minimum_score"])
        all_matches.extend(matches)

    if not prompt_matches and not all_matches:
        return None

    all_matches.sort(key=lambda m: m.score, reverse=True)
    selected: list[VectorMatch] = list(prompt_matches)
    token_budget = policy["max_tokens"] - sum(
        m.token_count or _approx_token_count(m.content) for m in prompt_matches
    )
    for match in all_matches[: policy["top_k"]]:
        tokens = match.token_count or _approx_token_count(match.content)
        if selected and token_budget - tokens < 0:
            break
        selected.append(match)
        token_budget -= tokens

    if not selected:
        return None

    return {
        "chunks": [
            {
                "content": m.content,
                "score": m.score,
                "source": {
                    "kb_id": m.kb_id,
                    "kb_slug": m.kb_slug,
                    "document_id": m.document_id,
                    "document_title": m.document_title,
                    "chunk_id": m.chunk_id,
                    "page": m.page,
                    "language": m.language,
                    "tags": m.tags,
                    "version": m.version,
                },
            }
            for m in selected
        ],
        "sources": list(dict.fromkeys(m.document_title for m in selected)),
        "token_count": sum(m.token_count or _approx_token_count(m.content) for m in selected),
        "include_citations": policy["include_citations"],
        "retrieval_metadata": {
            "top_k": policy["top_k"], "candidates": len(all_matches), "groups": len(groups),
            "rerank": policy["rerank"], "hybrid_search": policy["hybrid_search"],
            "always_included": len(prompt_matches),
        },
    }
