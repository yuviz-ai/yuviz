"""
Knowledge base CRUD — same audited-mutation pattern as
services/config/tenants.py. No Redis cache-aside here (unlike Config
Service's tenant/agent reads): KB rows are read rarely compared to the
retrieval hot path, which never reads this table directly anyway (see
retrieval.py — it goes straight from agent_knowledge_bases to kb_chunks).

Every connection is opened through tenant_conn()/platform_conn() (RLS design,
libs/tenancy) rather than a bare pool call. The by-id functions take
platform_scoped (source: deps.is_platform_scoped(current_user) only) and, for
a mutation on the platform branch, stamp_tenant — the tenant of the row the
caller's own router already fetched via _authorize_kb, so audit_log.tenant_id
still records the tenant whose row was edited even though the mutation itself
bypasses RLS.
"""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, db

_UPDATABLE_FIELDS = {"name", "description", "embedding_config_id", "status"}


async def get_knowledge_base(kb_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="kb-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_bases WHERE id = $1 AND deleted_at IS NULL", kb_id,
        )
    return dict(row) if row is not None else None


async def get_by_slug(tenant_id: Any, slug: str) -> dict[str, Any] | None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_bases WHERE tenant_id = $1 AND slug = $2 AND deleted_at IS NULL",
            tenant_id, slug,
        )
    return dict(row) if row is not None else None


async def list_knowledge_bases(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM knowledge_bases WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
            tenant_id,
        )
    return [dict(row) for row in rows]


async def create_knowledge_base(
    *,
    tenant_id: Any,
    slug: str,
    name: str,
    description: str = "",
    embedding_config_id: Any | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO knowledge_bases (tenant_id, slug, name, description, embedding_config_id) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING *",
            tenant_id, slug, name, description, embedding_config_id,
        )
        result = dict(row)
        await audit.write_audit(
            conn, entity_type="knowledge_base", entity_id=result["id"],
            action="created", user_id=user_id, user_email=user_email, new_value=result,
        )
    return result


async def update_knowledge_base(
    kb_id: Any,
    *,
    platform_scoped: bool = False,
    stamp_tenant: Any | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_knowledge_base() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_knowledge_base() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="kb-admin-by-id", stamp_tenant=stamp_tenant)
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        old_row = await conn.fetchrow("SELECT * FROM knowledge_bases WHERE id = $1 FOR UPDATE", kb_id)
        if old_row is None:
            raise LookupError(f"knowledge_base {kb_id} not found")
        old = dict(old_row)

        columns = list(fields.keys())
        set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
        new_row = await conn.fetchrow(
            f"UPDATE knowledge_bases SET {set_clause} WHERE id = $1 RETURNING *",
            kb_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

        await audit.write_audit(
            conn, entity_type="knowledge_base", entity_id=kb_id, action="updated",
            user_id=user_id, user_email=user_email, old_value=old, new_value=new,
        )
    return new


async def soft_delete_knowledge_base(
    kb_id: Any,
    *,
    platform_scoped: bool = False,
    stamp_tenant: Any | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="kb-admin-by-id", stamp_tenant=stamp_tenant)
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        old_row = await conn.fetchrow("SELECT * FROM knowledge_bases WHERE id = $1 FOR UPDATE", kb_id)
        if old_row is None:
            raise LookupError(f"knowledge_base {kb_id} not found")
        old = dict(old_row)

        await conn.execute("UPDATE knowledge_bases SET deleted_at = now() WHERE id = $1", kb_id)
        await audit.write_audit(
            conn, entity_type="knowledge_base", entity_id=kb_id, action="deleted",
            user_id=user_id, user_email=user_email, old_value=old,
        )
