"""campaigns CRUD — campaign lifecycle (draft -> running -> paused/completed); worker.py dials."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from libs.telephony_sdk.did_format import normalize_did
from libs.tenancy import platform_conn, tenant_conn

from . import audit, capacity, db


async def get_campaign(campaign_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM campaigns WHERE id = $1 AND deleted_at IS NULL", campaign_id,
        )
    return dict(row) if row is not None else None


async def get_campaign_for_caller(campaign_id: Any, tenant_id: Any | None) -> dict[str, Any] | None:
    """tenant_id None is a platform caller. A foreign campaign is indistinguishable from a missing one."""
    pool = await db.get_pool()
    conn_cm = tenant_conn(pool) if tenant_id is not None else platform_conn(pool, reason="campaign-by-id")
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM campaigns WHERE id = $1 AND deleted_at IS NULL AND ($2::uuid IS NULL OR tenant_id = $2::uuid)",
            campaign_id, tenant_id,
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


async def list_caller_ids(tenant_id: Any) -> list[dict[str, Any]]:
    """The tenant's DIDs with their outbound path: the bound telephony provider, else 'native'."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            """
            SELECT pn.did, COALESCE(tc.provider, 'native') AS path
            FROM phone_numbers pn
            LEFT JOIN telephony_configs tc
                   ON tc.id = pn.telephony_config_id AND tc.tenant_id = pn.tenant_id AND tc.deleted_at IS NULL
            WHERE pn.tenant_id = $1 AND pn.deleted_at IS NULL
            ORDER BY pn.did
            """,
            tenant_id,
        )
    return [dict(row) for row in rows]


async def agent_exists_for_tenant(tenant_id: Any, agent_id: Any) -> bool:
    """Checked up front so a bad/cross-tenant agent_id is a clean 422, not an asyncpg 500."""
    try:
        uuid.UUID(str(agent_id))
    except (ValueError, AttributeError, TypeError):
        return False
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM agents WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL",
            agent_id, tenant_id,
        )
    return row is not None


async def caller_id_owned_by_tenant(tenant_id: Any, caller_id: str | None) -> bool:
    """True if caller_id is a DID this tenant provisioned; empty is allowed (worker skips it)."""
    if not caller_id:
        return True
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM phone_numbers WHERE tenant_id = $1 AND did = $2 AND deleted_at IS NULL",
            tenant_id, normalize_did(caller_id),
        )
    return row is not None


async def agent_outbound_settings(
    tenant_id: Any, agent_id: Any, *, platform_scoped: bool = False,
) -> dict[str, Any] | None:
    """The agent's direction and outbound caller ID; None when the agent is not this tenant's."""
    try:
        uuid.UUID(str(agent_id))
    except (ValueError, AttributeError, TypeError):
        return None
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT direction, outbound_caller_id FROM agents WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL",
            agent_id, tenant_id,
        )
    return dict(row) if row else None


async def create_campaign(
    tenant_id: Any, *, agent_id: Any, name: str, caller_id: str,
    max_concurrent_calls: int, pacing_seconds: int, max_attempts: int,
    retry_gap_minutes: int = 60, calling_hours_start: str | None = None, calling_hours_end: str | None = None,
    calling_hours_timezone: str = "UTC", scheduled_start_at: datetime | None = None, description: str | None = None,
    user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    status = "scheduled" if scheduled_start_at else "draft"
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO campaigns "
            "(tenant_id, agent_id, name, status, caller_id, max_concurrent_calls, pacing_seconds, max_attempts, "
            "retry_gap_minutes, calling_hours_start, calling_hours_end, calling_hours_timezone, scheduled_start_at, "
            "description) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING *",
            tenant_id, agent_id, name, status, caller_id, max_concurrent_calls, pacing_seconds, max_attempts,
            retry_gap_minutes, calling_hours_start, calling_hours_end, calling_hours_timezone, scheduled_start_at,
            description,
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
    """start/pause/resume: worker.py polls for status='running', so this is the whole signal."""
    if status == "running":
        pool = await db.get_pool()
        conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
        async with conn_cm as conn:
            await conn.execute("UPDATE campaigns SET started_at = COALESCE(started_at, now()) WHERE id = $1", campaign_id)
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
                c.status, c.wait_reason, c.wait_since, t.max_outbound_concurrent_calls AS tenant_cap,
                LEAST(c.max_concurrent_calls, t.max_outbound_concurrent_calls) AS effective_limit,
                COUNT(cc.id) AS total,
                COUNT(cc.id) FILTER (WHERE cc.status = 'pending')   AS pending,
                COUNT(cc.id) FILTER (
                    WHERE cc.status = 'pending' AND (cc.next_attempt_at IS NULL OR cc.next_attempt_at <= now())
                )                                                   AS due,
                COUNT(cc.id) FILTER (WHERE cc.status = 'calling')   AS calling,
                COUNT(cc.id) FILTER (WHERE cc.status = 'completed') AS completed,
                COUNT(cc.id) FILTER (WHERE cc.status = 'failed')    AS failed,
                COUNT(cc.id) FILTER (WHERE cc.status = 'no_answer') AS no_answer,
                COUNT(cc.id) FILTER (WHERE cc.status = 'busy')      AS busy,
                COUNT(cc.id) FILTER (WHERE cc.status = 'declined')  AS declined,
                COUNT(cc.id) FILTER (WHERE cc.status = 'blocked')   AS blocked
            FROM campaigns c
            JOIN tenants t ON t.id = c.tenant_id
            LEFT JOIN campaign_contacts cc ON cc.campaign_id = c.id
            WHERE c.id = $1
            GROUP BY c.id, t.id
            """,
            campaign_id,
        )
    progress = dict(row)
    due = progress.pop("due")
    progress["waiting_count"] = due if progress["wait_reason"] is not None else 0
    return progress


async def resolve_outbound_route(
    tenant_id: Any, agent_id: Any, caller_id: str, *, platform_scoped: bool = False,
) -> dict[str, Any]:
    """Provider a caller_id DID is bound to, plus tenant/agent slugs and caller_id_owned.

    Gate on caller_id_owned, not provider: provider is None for both unowned and native/ESL DIDs."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            """
            SELECT t.slug AS tenant_slug, a.slug AS agent_slug, tc.provider AS provider, tc.id AS telephony_config_id,
                   pn.agent_id AS did_agent_id, (pn.id IS NOT NULL) AS caller_id_owned,
                   (a.tenant_id = t.id AND a.status = 'active' AND a.deleted_at IS NULL) AS agent_ok
            FROM tenants t
            JOIN agents a ON a.id = $2
            LEFT JOIN phone_numbers pn ON pn.tenant_id = t.id AND pn.did = $3 AND pn.deleted_at IS NULL
            LEFT JOIN telephony_configs tc ON tc.id = pn.telephony_config_id AND tc.tenant_id = t.id AND tc.deleted_at IS NULL
            WHERE t.id = $1
            """,
            tenant_id, agent_id, normalize_did(caller_id),
        )
    return dict(row)


async def is_finished(campaign_id: Any) -> bool:
    """Worker-side: every contact is terminal and no attempt is open (including an answered call that has not ended)."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        return await conn.fetchval(
            f"""
            SELECT NOT EXISTS (SELECT 1 FROM campaign_contacts WHERE campaign_id = $1 AND status IN ('pending', 'calling'))
               AND NOT EXISTS (SELECT 1 FROM campaign_attempts WHERE campaign_id = $1 AND {capacity.IN_FLIGHT})
            """,
            campaign_id,
        )


async def set_wait_reason(campaign_id: Any, reason: capacity.WaitReason | None) -> None:
    """Worker-side wait reason; touches the row only when it changes."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        await capacity.write_wait_reason(conn, campaign_id, reason)


CANCELLABLE = ("draft", "scheduled", "running", "paused")
RECALLABLE_OUTCOMES = ("no_answer", "busy", "failed", "declined")


async def set_cancelled(
    campaign: dict[str, Any], *, platform_scoped: bool = False, user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    if campaign["status"] not in CANCELLABLE:
        raise ValueError(f"a {campaign['status']} campaign can't be cancelled")
    updated = await update_campaign(
        campaign["id"], {"status": "cancelled"},
        platform_scoped=platform_scoped, user_id=user_id, user_email=user_email,
    )
    await close_unsent_attempts(campaign["id"], platform_scoped=platform_scoped)
    return updated


async def close_unsent_attempts(campaign_id: Any, *, platform_scoped: bool = False) -> int:
    """Attempts never handed to a provider can't be left open; calls already placed are left to finish."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        status = await conn.execute(
            "UPDATE campaign_attempts SET state = 'resolved', outcome = 'failed', failure_reason = 'campaign_cancelled', "
            "resolved_at = now() WHERE campaign_id = $1 AND state = 'pending'",
            campaign_id,
        )
    return int(status.split()[-1])


async def soft_delete_draft(
    campaign: dict[str, Any], *, platform_scoped: bool = False, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    if campaign["status"] != "draft":
        raise ValueError("only a draft campaign can be deleted; cancel it instead")
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        await conn.execute("UPDATE campaigns SET deleted_at = now() WHERE id = $1", campaign["id"])
        await audit.write_audit(
            conn, entity_type="campaign", entity_id=campaign["id"], action="deleted",
            user_id=user_id, user_email=user_email, old_value=campaign,
        )


async def list_call_attempts(
    tenant_id: Any, *, direction: str | None = None, status: str | None = None,
    query: str | None = None, limit: int = 200, offset: int = 0,
) -> list[dict[str, Any]]:
    """Campaign calls, newest first. Only outbound campaign calls exist here; the callee stays masked."""
    from .attempts import _mask_callee

    if direction not in (None, "", "outbound"):
        return []
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-attempts-list") as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.contact_id, a.campaign_id, c.name AS campaign_name, a.attempt, a.state, a.outcome,
                   a.failure_reason, a.callee, a.caller_id, a.created_at, a.answered_at, a.ended_at,
                   cc.status AS contact_status, ag.name AS agent_name
            FROM campaign_attempts a
            JOIN campaigns c ON c.id = a.campaign_id AND c.deleted_at IS NULL
            JOIN campaign_contacts cc ON cc.id = a.contact_id
            LEFT JOIN agents ag ON ag.id = c.agent_id
            WHERE a.tenant_id = $1
              AND ($2::text IS NULL OR $2 = 'all' OR
                   COALESCE(a.outcome, CASE WHEN a.state <> 'resolved' THEN 'in_progress' END) = $2)
              AND ($3::text IS NULL OR a.callee LIKE '%' || $3 || '%')
            ORDER BY a.created_at DESC
            LIMIT $4 OFFSET $5
            """,
            tenant_id, status or None, _digits(query) or None, limit, offset,
        )
    result = []
    for record in rows:
        row = dict(record)
        row["callee_masked"] = _mask_callee(row.pop("callee"))
        # Judged on the lead, not this attempt: a lead answered on a later try is not called again.
        row["can_call_again"] = row.pop("contact_status") in RECALLABLE_OUTCOMES
        result.append(row)
    return result


def _digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


async def recall(
    source: dict[str, Any], contact_ids: list[str], *, name: str | None,
    user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """A fresh draft with the same agent, caller ID and settings, holding only the chosen leads who were not reached."""
    from . import campaign_contacts

    wanted = set(contact_ids)
    contacts = await campaign_contacts.list_contacts(source["id"])
    eligible = [
        c for c in contacts
        if str(c["id"]) in wanted and c["status"] in RECALLABLE_OUTCOMES
    ]
    if not eligible:
        raise ValueError("none of the chosen leads can be called again (only no answer, busy, failed or declined)")
    new = await create_campaign(
        source["tenant_id"], agent_id=source["agent_id"],
        name=(name or f"Call again: {source['name']}").strip(),
        caller_id=source["caller_id"], max_concurrent_calls=source["max_concurrent_calls"],
        pacing_seconds=source["pacing_seconds"], max_attempts=source["max_attempts"],
        retry_gap_minutes=source["retry_gap_minutes"], calling_hours_start=source["calling_hours_start"],
        calling_hours_end=source["calling_hours_end"], calling_hours_timezone=source["calling_hours_timezone"],
        description=f"Re-dial of {source['name']}", user_id=user_id, user_email=user_email,
    )
    await campaign_contacts.bulk_insert_contacts(
        new["id"], [{"phone_number": c["phone_number"], "name": c["name"] or ""} for c in eligible],
        tenant_id=source["tenant_id"],
    )
    return new
