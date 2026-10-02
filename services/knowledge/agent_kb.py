"""agent_knowledge_bases CRUD — sole writer of the agent_kb:{tenant}:{agent} Redis flag.
Every mutation rewrites the flag (write-through, no TTL)."""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import cache, db


async def get_agent_tenant_id(agent_id: Any, *, platform_scoped: bool = False) -> str | None:
    """Tenant of agent_id, for authorizing junction-table routes (which have no tenant column)."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="agent-kb-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM agents WHERE id = $1 AND deleted_at IS NULL", agent_id,
        )
    return str(row["tenant_id"]) if row is not None else None


async def _tenant_and_agent_slugs(agent_id: Any) -> tuple[str, str] | None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
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
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
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
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT akb.*, kb.slug AS kb_slug, kb.name AS kb_name FROM agent_knowledge_bases akb "
            "JOIN knowledge_bases kb ON kb.id = akb.kb_id AND kb.deleted_at IS NULL "
            "WHERE akb.agent_id = $1 ORDER BY kb.name",
            agent_id,
        )
    return [dict(row) for row in rows]


async def list_for_kb(kb_id: Any, *, platform_scoped: bool = False) -> list[dict[str, Any]]:
    """Agents linked to kb_id; platform_scoped must come from deps.is_platform_scoped()."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="kb-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        rows = await conn.fetch(
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
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO agent_knowledge_bases (agent_id, kb_id, enabled) VALUES ($1, $2, $3) "
            "ON CONFLICT (agent_id, kb_id) DO UPDATE SET enabled = $3 RETURNING *",
            agent_id, kb_id, enabled,
        )
        result = dict(row)
    await _refresh_flag(agent_id)
    return result


async def set_enabled(agent_id: Any, kb_id: Any, *, enabled: bool) -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
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
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "DELETE FROM agent_knowledge_bases WHERE agent_id = $1 AND kb_id = $2", agent_id, kb_id,
        )
    await _refresh_flag(agent_id)


async def has_enabled_kb(tenant_slug: str, agent_slug: str) -> bool:
    """Postgres fallback for a Redis miss, by slugs; the router sets the target tenant first."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
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
