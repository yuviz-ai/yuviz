"""
audit_log writer — always called inside the same transaction as the mutation
it's recording, so a config change and its audit entry commit or roll back
together (never a write that "succeeded" with no trace, or an orphaned audit
row for a write that failed).

Redacts known secret-reference fields before the value ever reaches
Postgres. This is defense in depth: api_key_ref/auth_token_ref are already
just reference paths, not resolved secrets — but redacting them in the audit
trail means a leaked audit_log row never reveals which Vault/K8s path to
target next, on top of never containing a live key. password_hash is
redacted too — a bcrypt hash isn't the plaintext password, but there's no
reason for it to sit in a JSONB audit column either.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import asyncpg

from libs.tenancy import platform_conn, tenant_conn

from . import db

_SECRET_REF_FIELDS = {"api_key_ref", "auth_token_ref", "password_hash", "token_hash"}


def _redact(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {k: ("[redacted]" if k in _SECRET_REF_FIELDS else v) for k, v in value.items()}


async def write_audit(
    conn: asyncpg.Connection,
    *,
    entity_type: str,
    entity_id: Any,
    action: Literal["created", "updated", "deleted"],
    user_id: Any | None = None,
    user_email: str | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_log "
        "(entity_type, entity_id, user_id, user_email, action, old_value, new_value, ip_address) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)",
        entity_type,
        entity_id,
        user_id,
        user_email,
        action,
        json.dumps(_redact(old_value), default=str) if old_value is not None else None,
        json.dumps(_redact(new_value), default=str) if new_value is not None else None,
        ip_address,
    )


async def list_audit_log(
    *,
    tenant_id: str | None,
    platform_scoped: bool = False,
    entity_type: str | None = None,
    entity_id: str | None = None,
    user_email: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """`tenant_id` + `platform_scoped` follow the `users.py:55 list_users`
    convention: the route derives both from `deps.is_platform_scoped(current_user)`
    (lesson 24) — a tenant-scoped superadmin gets an explicit
    `AND tenant_id = $n` predicate on top of tenant_conn()'s own RLS scope
    (app layer and RLS both, never RLS alone); a platform-scoped caller gets
    `platform_conn()` with no tenant predicate, reading every tenant's rows
    exactly as before this fix."""
    pool = await db.get_pool()
    where: list[str] = []
    params: list[Any] = []

    if entity_type is not None:
        params.append(entity_type)
        where.append(f"entity_type = ${len(params)}")
    if entity_id is not None:
        params.append(entity_id)
        where.append(f"entity_id = ${len(params)}")
    if action is not None:
        params.append(action)
        where.append(f"action = ${len(params)}")
    if user_email is not None:
        params.append(f"%{user_email}%")
        where.append(f"user_email ILIKE ${len(params)}")
    if not platform_scoped:
        params.append(tenant_id)
        where.append(f"tenant_id = ${len(params)}")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    conn_cm = platform_conn(pool, reason="audit-log-platform-read") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM audit_log {where_clause}", *params)

        params.extend([limit, offset])
        rows = await conn.fetch(
            f"SELECT * FROM audit_log {where_clause} "
            f"ORDER BY changed_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(row) for row in rows],
    }
