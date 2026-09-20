"""
retrieve() — the one function backing POST /internal/retrieve, the single
call Conversation Service's Knowledge SDK makes per user turn.

_resolve_policy() is the "never hardcode retrieval knobs" mechanism: three
tiers, most specific wins —
    1. this call's explicit override (a non-None field on RetrieveRequest)
    2. the agent's configured agent_retrieval_policies row
    3. _SYSTEM_DEFAULT_POLICY (the one place a fixed number is allowed to
       live, and only as the bottom of an explicit, visible chain — same
       "agent override > tenant default" pattern libs.config_sdk's
       CacheAsideConfigProvider already uses for provider config
       resolution).

Steps after that: find the agent's enabled+active KBs -> group them by
embedding_config_id (an agent may attach KBs embedded with different
providers; querying each group needs that group's own query embedding) ->
embed the query once per group -> IVectorRepository.search() per group ->
merge all groups' matches by score -> apply the resolved policy (top_k,
minimum_score already applied per-group; max_tokens truncates the merged,
sorted list here) -> shape the raw dict libs.knowledge_sdk.
HttpKnowledgeRepository.retrieve() expects.

usage_mode='prompt' documents (see kb_documents.usage_mode) are fetched
separately via _fetch_prompt_mode_matches() and always included ahead of
similarity-ranked matches, regardless of query relevance or top_k/
minimum_score — they never go through PgVectorRepository.search() at all
(it explicitly excludes them; see vector_repository.py), so a document
that was never embedded (auto-inlined for being under
ingestion_worker.AUTO_INLINE_THRESHOLD_BYTES) still surfaces correctly.

Returns None when the agent has no eligible KB, or has eligible KBs but
neither an always-include document nor a chunk clearing minimum_score —
the router turns that into a 404, which the SDK's repository already
treats as "no context" (see http_repository.py).
"""

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
    """Returns {embedding_config_id: [kb_id, ...]} for this agent's
    enabled, active KBs — the grouping retrieve() needs before it can embed
    the query even once."""
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
    """Every enabled, active KB attached to this agent — unlike
    _agent_kb_groups(), does NOT require embedding_config_id: a KB holding
    only usage_mode='prompt' documents needs no embedding provider at all,
    since those documents are never vector-searched."""
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
    """usage_mode='prompt' documents across kb_ids, one VectorMatch per
    document (chunks reassembled in order) — score=1.0 is a sentinel
    meaning "always relevant, not similarity-ranked", never compared
    against minimum_score. page/language/tags/version are taken from the
    document's first chunk; a document flagged 'prompt' rarely has more
    than one (auto-inlined documents always have exactly one — see
    ingestion_worker.py), so this is a reasonable representative value,
    not a lossy average."""
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

    # Always-include documents first — never subject to top_k or
    # minimum_score, and never truncated by the token budget (same
    # "admin's explicit choice, not silently dropped" posture the prompt-
    # mode feature is documented under — see module docstring). Fetched
    # regardless of whether any KB here has an embedding provider at all.
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
