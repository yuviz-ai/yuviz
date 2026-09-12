"""
campaigns CRUD — outbound calling campaign lifecycle (draft -> running ->
paused/completed). Placing actual calls is worker.py's job, not this
module's — this only owns the campaign row itself, mirroring the
"routers/CRUD modules translate, a separate process does the work" split
already used by services/knowledge/'s ingestion worker.
"""

from __future__ import annotations

import uuid
from typing import Any

from . import audit, db


async def get_campaign(campaign_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM campaigns WHERE id = $1 AND deleted_at IS NULL", campaign_id,
    )
    return dict(row) if row is not None else None


async def list_campaigns(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT * FROM campaigns WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY created_at DESC",
        tenant_id,
    )
    return [dict(row) for row in rows]


async def agent_exists_for_tenant(tenant_id: Any, agent_id: Any) -> bool:
    """campaigns.agent_id is a NOT NULL FK (database/schema.sql) — an empty
    string or another tenant's agent id previously reached the INSERT
    unchecked and surfaced as an unhandled asyncpg UUID-cast/FK-violation
    exception (a bare 500 with no CORS headers, since the exception occurs
    after CORSMiddleware's request phase — Chrome then misreports the
    response as CORS-blocked rather than a server error). Checked up front
    so the router can raise a clean, CORS-intact 422 instead."""
    try:
        uuid.UUID(str(agent_id))
    except (ValueError, AttributeError, TypeError):
        # Not even UUID-shaped (e.g. "") — the query below would itself
        # raise the same uncaught asyncpg cast error this check exists to
        # avoid, so short-circuit before it ever reaches the database.
        return False
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM agents WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL",
        agent_id, tenant_id,
    )
    return row is not None


async def create_campaign(
    tenant_id: Any, *, agent_id: Any, name: str, caller_id: str | None,
    max_concurrent_calls: int, pacing_seconds: int, max_attempts: int,
    calling_hours_start: str | None = None, calling_hours_end: str | None = None,
    calling_hours_timezone: str = "UTC",
    user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO campaigns "
                "(tenant_id, agent_id, name, caller_id, max_concurrent_calls, pacing_seconds, max_attempts, "
                "calling_hours_start, calling_hours_end, calling_hours_timezone) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING *",
                tenant_id, agent_id, name, caller_id, max_concurrent_calls, pacing_seconds, max_attempts,
                calling_hours_start, calling_hours_end, calling_hours_timezone,
            )
            result = dict(row)
            await audit.write_audit(
                conn, entity_type="campaign", entity_id=result["id"], action="created",
                user_id=user_id, user_email=user_email, new_value=result,
            )
    return result


async def update_campaign(
    campaign_id: Any, fields: dict[str, Any], *, user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        existing = await get_campaign(campaign_id)
        if existing is None:
            raise LookupError(f"campaign {campaign_id} not found")
        return existing

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM campaigns WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", campaign_id,
            )
            if old_row is None:
                raise LookupError(f"campaign {campaign_id} not found")
            old = dict(old_row)

            set_clauses = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(fields))
            new_row = await conn.fetchrow(
                f"UPDATE campaigns SET {set_clauses}, updated_at = now() WHERE id = $1 RETURNING *",
                campaign_id, *fields.values(),
            )
            new = dict(new_row)

            await audit.write_audit(
                conn, entity_type="campaign", entity_id=campaign_id, action="updated",
                user_id=user_id, user_email=user_email, old_value=old, new_value=new,
            )
    return new


async def set_status(
    campaign_id: Any, status: str, *, user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """start/pause/resume are all just status transitions — worker.py polls
    for status='running' campaigns, so flipping this is the entire
    "start/pause/resume" API surface; no separate start/stop signal needed."""
    return await update_campaign(campaign_id, {"status": status}, user_id=user_id, user_email=user_email)


async def get_progress(campaign_id: Any) -> dict[str, Any]:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'pending')   AS pending,
            COUNT(*) FILTER (WHERE status = 'calling')   AS calling,
            COUNT(*) FILTER (WHERE status = 'completed') AS completed,
            COUNT(*) FILTER (WHERE status = 'failed')    AS failed,
            COUNT(*) FILTER (WHERE status = 'no_answer') AS no_answer,
            COUNT(*) FILTER (WHERE status = 'blocked')   AS blocked
        FROM campaign_contacts WHERE campaign_id = $1
        """,
        campaign_id,
    )
    return dict(row)
