"""purchased_numbers CRUD — numbers bought on a tenant's behalf (purchase lifecycle, not routing).
Never writes phone_numbers; Config Service owns DID->agent assignment."""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, db


async def get_purchased_number(
    purchased_number_id: Any, *, platform_scoped: bool = False,
) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_ctx = (
        platform_conn(pool, reason="did-purchased-number-by-id")
        if platform_scoped
        else tenant_conn(pool)
    )
    async with conn_ctx as conn:
        row = await conn.fetchrow(
            "SELECT * FROM purchased_numbers WHERE id = $1 AND released_at IS NULL", purchased_number_id,
        )
    return dict(row) if row is not None else None


async def list_purchased_numbers(tenant_id: Any) -> list[dict[str, Any]]:
    """Unreleased numbers only."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM purchased_numbers WHERE tenant_id = $1 AND released_at IS NULL ORDER BY purchased_at DESC",
            tenant_id,
        )
    return [dict(row) for row in rows]


async def record_purchase(
    *,
    tenant_id: Any,
    carrier_id: Any,
    phone_number: str,
    carrier_number_sid: str,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Record a purchase that already succeeded at the carrier."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO purchased_numbers (tenant_id, carrier_id, phone_number, carrier_number_sid) "
            "VALUES ($1, $2, $3, $4) RETURNING *",
            tenant_id, carrier_id, phone_number, carrier_number_sid,
        )
        result = dict(row)
        await audit.write_audit(
            conn,
            entity_type="purchased_number",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value=result,
        )
    return result


async def record_assignment(purchased_number_id: Any, phone_number_id: Any) -> None:
    """Link to the phone_numbers row created via Config Service (no cross-service transaction).
    Runs on the ambient tenant scope the caller already set."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "UPDATE purchased_numbers SET phone_number_id = $2 WHERE id = $1", purchased_number_id, phone_number_id,
        )


async def record_release(
    purchased_number_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """Mark released after the carrier release succeeded; runs on the caller's ambient tenant scope."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM purchased_numbers WHERE id = $1 FOR UPDATE", purchased_number_id,
        )
        if old_row is None:
            raise LookupError(f"purchased_number {purchased_number_id} not found")
        old = dict(old_row)

        new_row = await conn.fetchrow(
            "UPDATE purchased_numbers SET released_at = now() WHERE id = $1 RETURNING *",
            purchased_number_id,
        )
        new = dict(new_row)

        await audit.write_audit(
            conn,
            entity_type="purchased_number",
            entity_id=purchased_number_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
            new_value=new,
        )
    return new
