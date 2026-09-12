"""
agent_knowledge_bases CRUD — the one write path responsible for keeping
libs.knowledge_sdk.RedisKnowledgeRepository's agent_kb:{tenant_slug}:
{agent_slug} flag correct (one writer per key, write-through, no TTL — same
principle as Config Service's phone_numbers.py DID cache). Every mutation
here recomputes and rewrites that flag in the same call, so a Conversation
Service instance never has to wait out a TTL to see a KB attach/detach/
enable/disable take effect.
"""

from __future__ import annotations

from typing import Any

from . import cache, db


async def _tenant_and_agent_slugs(agent_id: Any) -> tuple[str, str] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT a.slug AS agent_slug, t.slug AS tenant_slug "
        "FROM agents a JOIN tenants t ON t.id = a.tenant_id WHERE a.id = $1",
        agent_id,
    )
    return (row["tenant_slug"], row["agent_slug"]) if row is not None else None


async def _refresh_flag(agent_id: Any) -> None:
    slugs = await _tenant_and_agent_slugs(agent_id)
    if slugs is None:
        return
    tenant_slug, agent_slug = slugs
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT EXISTS ("
        "  SELECT 1 FROM agent_knowledge_bases akb "
        "  JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.deleted_at IS NULL AND kb.status = 'active' "
        "  WHERE akb.agent_id = $1 AND akb.enabled"
        ") AS has_kb",
        agent_id,
    )
    await cache.set_has_enabled_kb(tenant_slug, agent_slug, bool(row["has_kb"]))


async def list_for_agent(agent_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT akb.*, kb.slug AS kb_slug, kb.name AS kb_name FROM agent_knowledge_bases akb "
        "JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.deleted_at IS NULL "
        "WHERE akb.agent_id = $1 ORDER BY kb.name",
        agent_id,
    )
    return [dict(row) for row in rows]


async def list_for_kb(kb_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT akb.agent_id, akb.kb_id, akb.enabled, akb.created_at, "
        "       a.slug AS agent_slug, a.name AS agent_name, a.tenant_id "
        "FROM agent_knowledge_bases akb "
        "JOIN agents a  ON a.id = akb.agent_id  AND a.deleted_at IS NULL "
        "JOIN tenants t ON t.id = a.tenant_id   AND t.deleted_at IS NULL "
        "WHERE akb.kb_id = $1 "
        "ORDER BY a.name",
        kb_id,
    )
    return [dict(row) for row in rows]


async def assign(agent_id: Any, kb_id: Any, *, enabled: bool = True) -> dict[str, Any]:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "INSERT INTO agent_knowledge_bases (agent_id, kb_id, enabled) VALUES ($1, $2, $3) "
        "ON CONFLICT (agent_id, kb_id) DO UPDATE SET enabled = $3 RETURNING *",
        agent_id, kb_id, enabled,
    )
    result = dict(row)
    await _refresh_flag(agent_id)
    return result


async def set_enabled(agent_id: Any, kb_id: Any, *, enabled: bool) -> dict[str, Any]:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "UPDATE agent_knowledge_bases SET enabled = $3 WHERE agent_id = $1 AND kb_id = $2 RETURNING *",
        agent_id, kb_id, enabled,
    )
    if row is None:
        raise LookupError(f"agent_knowledge_bases ({agent_id}, {kb_id}) not found")
    result = dict(row)
    await _refresh_flag(agent_id)
    return result


async def detach(agent_id: Any, kb_id: Any) -> None:
    pool = await db.get_pool()
    await pool.execute(
        "DELETE FROM agent_knowledge_bases WHERE agent_id = $1 AND kb_id = $2", agent_id, kb_id,
    )
    await _refresh_flag(agent_id)


async def has_enabled_kb(tenant_slug: str, agent_slug: str) -> bool:
    """The HTTP-repository fallback for a Redis miss (see
    libs.knowledge_sdk.HttpKnowledgeRepository / cache_aside.py) — the one
    place this check is computed straight from Postgres by (tenant_slug,
    agent_slug) rather than by agent_id, since that's the shape the SDK
    caller (Conversation Service) actually has on hand."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT EXISTS ("
        "  SELECT 1 FROM agent_knowledge_bases akb "
        "  JOIN agents a ON a.id = akb.agent_id AND a.deleted_at IS NULL "
        "  JOIN tenants t ON t.id = a.tenant_id AND t.deleted_at IS NULL "
        "  JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.deleted_at IS NULL AND kb.status = 'active' "
        "  WHERE t.slug = $1 AND a.slug = $2 AND akb.enabled"
        ") AS has_kb",
        tenant_slug, agent_slug,
    )
    return bool(row["has_kb"])
