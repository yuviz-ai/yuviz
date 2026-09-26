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

from libs.tenancy import platform_conn, tenant_conn

from . import audit, db


async def get_campaign(campaign_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM campaigns WHERE id = $1 AND deleted_at IS NULL", campaign_id,
        )
    return dict(row) if row is not None else None


async def list_campaigns(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
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
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM agents WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL",
            agent_id, tenant_id,
        )
    return row is not None


async def caller_id_owned_by_tenant(tenant_id: Any, caller_id: str | None) -> bool:
    """A `caller_id` a campaign dials out with must be a DID this tenant
    actually provisioned (`phone_numbers.did`) — same tenant-ownership
    predicate `services/telephony/ownership.py`'s outbound-trigger path
    enforces (there, via the Redis `did:{did}` cache; here, cold-path
    against Postgres directly, matching `agent_exists_for_tenant`'s
    existing shape). `caller_id` is optional on a campaign row
    (`None` means "not yet configured, cannot start") — that case is
    valid at create/update time and is instead caught by worker.py's own
    "no caller_id configured, skipping" guard before any dial."""
    if not caller_id:
        return True
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM phone_numbers WHERE tenant_id = $1 AND did = $2 AND deleted_at IS NULL",
            tenant_id, caller_id,
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
    async with tenant_conn(pool) as conn:
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
    campaign_id: Any, fields: dict[str, Any], *,
    platform_scoped: bool = False, user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        existing = await get_campaign(campaign_id, platform_scoped=platform_scoped)
        if existing is None:
            raise LookupError(f"campaign {campaign_id} not found")
        return existing

    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
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
    campaign_id: Any, status: str, *,
    platform_scoped: bool = False, user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """start/pause/resume are all just status transitions — worker.py polls
    for status='running' campaigns, so flipping this is the entire
    "start/pause/resume" API surface; no separate start/stop signal needed."""
    return await update_campaign(
        campaign_id, {"status": status}, platform_scoped=platform_scoped, user_id=user_id, user_email=user_email,
    )


async def get_progress(campaign_id: Any, *, platform_scoped: bool = False) -> dict[str, Any]:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
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


async def resolve_outbound_route(
    tenant_id: Any, agent_id: Any, caller_id: str, *, platform_scoped: bool = False,
) -> dict[str, Any]:
    """Which telephony provider a caller_id DID is bound to, plus the tenant/agent
    slugs a REST provider (Vobiz) needs.

    `caller_id_owned` is the tenant-ownership fact worker.py's dispatch
    must gate on: True only when `caller_id` is an actual
    `phone_numbers.did` row for THIS tenant. `provider` is a second,
    separate fact — None either for an unowned caller_id OR for an owned
    one with no REST `telephony_configs` binding (a legitimate native/ESL
    DID) — so the two must never be conflated: a caller_id this tenant
    never provisioned must be refused outright, never silently routed to
    the ESL path, while an owned-but-native DID must still dial."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            """
            SELECT t.slug AS tenant_slug, a.slug AS agent_slug, tc.provider AS provider,
                   (pn.id IS NOT NULL) AS caller_id_owned
            FROM tenants t
            JOIN agents a ON a.id = $2
            LEFT JOIN phone_numbers pn ON pn.tenant_id = t.id AND pn.did = $3 AND pn.deleted_at IS NULL
            LEFT JOIN telephony_configs tc ON tc.id = pn.telephony_config_id AND tc.tenant_id = t.id AND tc.deleted_at IS NULL
            WHERE t.id = $1
            """,
            tenant_id, agent_id, caller_id,
        )
    return dict(row)
