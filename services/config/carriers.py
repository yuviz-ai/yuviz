"""Carriers (BYOC) CRUD — cold path, uncached.

auth_token_ref is a reference path (e.g. 'env:PLIVO_AUTH_TOKEN'), safe to return.
"""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, db

_UPDATABLE_FIELDS = {"name", "auth_id", "auth_token_ref", "carrier_account_ref"}


async def get_carrier_by_id(carrier_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """platform_scoped (from deps.is_platform_scoped only) selects platform_conn."""
    pool = await db.get_pool()
    if platform_scoped:
        async with platform_conn(pool, reason="carriers-by-id") as conn:
            row = await conn.fetchrow(
                "SELECT * FROM carriers WHERE id = $1 AND deleted_at IS NULL", carrier_id,
            )
    else:
        async with tenant_conn(pool) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM carriers WHERE id = $1 AND deleted_at IS NULL", carrier_id,
            )
    return dict(row) if row is not None else None


async def list_carriers(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM carriers WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
            tenant_id,
        )
    return [dict(row) for row in rows]


async def create_carrier(
    *,
    tenant_id: Any,
    name: str,
    provider: str,
    auth_id: str | None = None,
    auth_token_ref: str | None = None,
    carrier_account_ref: str | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO carriers (tenant_id, name, provider, auth_id, auth_token_ref, carrier_account_ref) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            tenant_id, name, provider, auth_id, auth_token_ref, carrier_account_ref,
        )
        result = dict(row)
        await audit.write_audit(
            conn,
            entity_type="carrier",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value=result,
        )
    return result


async def update_carrier(
    carrier_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_carrier() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_carrier() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM carriers WHERE id = $1 FOR UPDATE", carrier_id,
        )
        if old_row is None:
            raise LookupError(f"carrier {carrier_id} not found")
        old = dict(old_row)

        columns = list(fields.keys())
        set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
        new_row = await conn.fetchrow(
            f"UPDATE carriers SET {set_clause}, updated_at = now() WHERE id = $1 RETURNING *",
            carrier_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

        # Written columns only, so a redacted unchanged auth_token_ref isn't logged as changed.
        await audit.write_audit(
            conn,
            entity_type="carrier",
            entity_id=carrier_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value={col: old[col] for col in columns},
            new_value={col: new[col] for col in columns},
        )
    return new


async def soft_delete_carrier(
    carrier_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM carriers WHERE id = $1 FOR UPDATE", carrier_id,
        )
        if old_row is None:
            raise LookupError(f"carrier {carrier_id} not found")
        old = dict(old_row)

        await conn.execute(
            "UPDATE carriers SET deleted_at = now() WHERE id = $1", carrier_id,
        )
        await audit.write_audit(
            conn,
            entity_type="carrier",
            entity_id=carrier_id,
            action="deleted",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
        )
