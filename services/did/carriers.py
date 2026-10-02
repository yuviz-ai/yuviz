"""Read-only carriers accessor for DID Service; services/config owns carrier CRUD."""

from __future__ import annotations

from typing import Any

from libs.tenancy import tenant_conn

from . import db


async def get_carrier_by_id(carrier_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM carriers WHERE id = $1 AND deleted_at IS NULL", carrier_id,
        )
    return dict(row) if row is not None else None
