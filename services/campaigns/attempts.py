"""campaign_attempts writes. Every state or outcome move is one conditional UPDATE ... RETURNING."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import asyncpg

from libs.tenancy import platform_conn

from . import capacity, db

Path = Literal["sip", "cpaas"]

ADMISSION_LOCK = "SELECT pg_advisory_xact_lock(hashtext('campaigns:admission'))"
DEADLINE_S = 120
RESEND_WINDOW_S = 60
MAX_SENDS = 2

_OUTCOMES = ("answered", "ended", "busy", "no_answer", "declined", "failed")


def idempotency_key(contact_id: str | UUID, attempt: int) -> str:
    return f"{contact_id}_{attempt}"


async def create_attempt(
    conn: asyncpg.Connection, *, campaign: dict[str, Any], contact: dict[str, Any],
    path: Path, provider: str | None, cps_scope: str,
) -> dict[str, Any]:
    """Insert the first-send reservation; the attempt number is max+1 for the contact, so keys stay unique."""
    row = await conn.fetchrow(
        """
        WITH n AS (SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt FROM campaign_attempts WHERE contact_id = $3)
        INSERT INTO campaign_attempts
            (tenant_id, campaign_id, contact_id, attempt, idempotency_key, path, provider, callee, caller_id,
             call_session_id, cps_scope, send_count, last_send_at, deadline_at, max_call_s, billing_state)
        SELECT $1, $2, $3, n.attempt, $3::text || '_' || n.attempt, $4, $5, $6, $7,
               CASE WHEN $4 = 'sip' THEN gen_random_uuid()::text END, $8, 1, clock_timestamp(),
               now() + make_interval(secs => $9),
               (SELECT GREATEST(30, LEAST(7200, COALESCE(a.max_call_duration_s, 7200)))
                  FROM agents a WHERE a.id = $10 AND a.tenant_id = $1),
               'off'
        FROM n
        RETURNING *
        """,
        campaign["tenant_id"], campaign["id"], contact["id"], path, provider, contact["phone_number"],
        campaign["caller_id"], cps_scope, DEADLINE_S, campaign["agent_id"],
    )
    return dict(row)


async def get_attempt(tenant_id: UUID, key: str) -> dict[str, Any] | None:
    """The tenant's attempt for a dial key, with its tenant slug; None for an unknown key or another tenant's."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        row = await conn.fetchrow(
            "SELECT a.*, t.slug AS tenant_slug FROM campaign_attempts a JOIN tenants t ON t.id = a.tenant_id "
            "WHERE a.idempotency_key = $1 AND a.tenant_id = $2",
            key, tenant_id,
        )
    return dict(row) if row is not None else None


def _mask_callee(value: str | None) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return "•••" + digits[-4:] if len(digits) > 4 else "•••"


async def list_for_campaign(campaign_id: UUID, tenant_id: UUID) -> list[dict[str, Any]]:
    """Attempt history for the API: the callee is masked and there is no caller ID."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-by-id") as conn:
        rows = await conn.fetch(
            "SELECT a.id, a.contact_id, a.attempt, a.path, a.provider, a.state, a.outcome, a.closed_by_deadline, "
            "a.failure_reason, a.send_count, a.created_at, a.sent_at, a.answered_at, a.resolved_at, a.ended_at, "
            "a.cost_minor, a.cost_currency, a.callee, "
            "CASE WHEN a.answered_at IS NOT NULL THEN k.disposition END AS disposition "
            "FROM campaign_attempts a JOIN tenants t ON t.id = a.tenant_id "
            "LEFT JOIN calls k ON k.session_id = a.call_session_id AND k.tenant_id = t.slug "
            "WHERE a.campaign_id = $1 AND a.tenant_id = $2 ORDER BY a.created_at, a.attempt",
            campaign_id, tenant_id,
        )
    result = []
    for record in rows:
        row = dict(record)
        row["callee_masked"] = _mask_callee(row.pop("callee"))
        result.append(row)
    return result


async def list_open(limit: int = 200) -> list[dict[str, Any]]:
    """Unresolved attempts first, then still-answered ones, with what reconcile needs; time tests run in SQL on one clock."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        rows = await conn.fetch(
            """
            SELECT a.*, t.slug AS tenant_slug, ag.slug AS agent_slug,
                   a.deadline_at <= now() AS past_deadline,
                   now() - a.created_at > interval '10 seconds' AS resend_age,
                   (a.not_before IS NULL OR a.not_before <= now()) AS send_due,
                   a.answered_at + make_interval(secs => COALESCE(ag.max_call_duration_s, 7200) + 300) <= now() AS end_overdue,
                   cl.session_id IS NOT NULL AS has_call, cl.ended_at AS call_ended_at
            FROM campaign_attempts a
            JOIN tenants t ON t.id = a.tenant_id
            JOIN campaigns c ON c.id = a.campaign_id AND c.tenant_id = a.tenant_id
            LEFT JOIN agents ag ON ag.id = c.agent_id AND ag.tenant_id = a.tenant_id
            LEFT JOIN calls cl ON cl.session_id = a.call_session_id AND cl.tenant_id = t.slug
            WHERE a.state <> 'resolved' OR a.outcome = 'answered'
            ORDER BY (a.state = 'resolved'), a.created_at LIMIT $1
            """,
            limit,
        )
    return [dict(row) for row in rows]


async def defer_send(attempt_id: UUID, *, delay_s: float) -> bool:
    """Hold a pending attempt back (Path B rate limit) and move its deadline past the hold."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        row = await conn.fetchrow(
            "UPDATE campaign_attempts SET not_before = now() + make_interval(secs => $2), "
            "deadline_at = now() + make_interval(secs => $2 + $3) WHERE id = $1 AND state = 'pending' RETURNING id",
            attempt_id, delay_s, DEADLINE_S,
        )
    return row is not None


async def mark_sent(attempt_id: UUID, *, provider_call_id: str | None) -> bool:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        row = await conn.fetchrow(
            "UPDATE campaign_attempts SET state = 'sent', sent_at = now(), "
            "provider_call_id = COALESCE($2, provider_call_id) WHERE id = $1 AND state = 'pending' RETURNING id",
            attempt_id, provider_call_id,
        )
    return row is not None


async def reserve_send(attempt_id: UUID, *, path: Path) -> Literal["ok", "wait_cps", "refused"]:
    """Gate before every re-send. Takes the admission lock so the CPS window cannot be over-booked."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        await conn.execute(ADMISSION_LOCK)
        scope = await conn.fetchval("SELECT cps_scope FROM campaign_attempts WHERE id = $1", attempt_id)
        if scope is None:
            return "refused"
        if await capacity.cps_exhausted(conn, scope):
            return "wait_cps"
        row = await conn.fetchrow(
            "UPDATE campaign_attempts SET send_count = send_count + 1, last_send_at = clock_timestamp() "
            "WHERE id = $1 AND state = 'pending' "
            "AND ($2 <> 'sip' OR (send_count < $3 AND created_at > now() - make_interval(secs => $4))) "
            "RETURNING id",
            attempt_id, path, MAX_SENDS, RESEND_WINDOW_S,
        )
    return "ok" if row is not None else "refused"


async def apply_outcome(
    *, tenant_id: UUID | None, key: str | None, provider_call_id: str | None, outcome: str,
    call_session_id: str | None, closed_by_deadline: bool = False, failure_reason: str | None = None,
) -> dict[str, Any] | None:
    """Apply a call outcome and mirror it onto the contact. None when nothing matched: duplicate,
    out of order, or another tenant's key. Only answered -> ended and a deadline failure upgraded by
    late answered/ended evidence move an already-resolved attempt."""
    if outcome not in _OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-dial-state") as conn:
        attempt = await conn.fetchrow(
            """
            UPDATE campaign_attempts SET
                state = 'resolved', outcome = $5,
                closed_by_deadline = $6,
                failure_reason = CASE WHEN failure_reason = 'superseded' THEN failure_reason
                                      WHEN $7::text IS NOT NULL THEN $7
                                      WHEN outcome = 'failed' THEN NULL ELSE failure_reason END,
                call_session_id = COALESCE($4, call_session_id),
                provider_call_id = COALESCE($3, provider_call_id),
                answered_at = CASE WHEN $5 IN ('answered', 'ended') THEN COALESCE(answered_at, now())
                                   ELSE answered_at END,
                resolved_at = COALESCE(resolved_at, now()),
                ended_at = CASE WHEN $5 = 'ended' THEN now() ELSE ended_at END
            WHERE (idempotency_key = $2 OR (provider_call_id = $3 AND tenant_id = $1))
              AND ($1::uuid IS NULL OR tenant_id = $1)
              AND (state <> 'resolved'
                   OR (outcome = 'answered' AND $5 = 'ended')
                   OR (outcome = 'failed' AND closed_by_deadline AND $5 IN ('answered', 'ended')))
            RETURNING *
            """,
            tenant_id, key, provider_call_id, call_session_id, outcome, closed_by_deadline, failure_reason,
        )
        if attempt is None:
            return None
        await _mirror_contact(conn, dict(attempt))
    return dict(attempt)


async def _mirror_contact(conn: asyncpg.Connection, attempt: dict[str, Any]) -> None:
    if attempt["failure_reason"] == "superseded":
        return  # the contact was already completed by an earlier attempt's late answer
    if attempt["outcome"] in ("answered", "ended"):
        await conn.execute(
            "UPDATE campaign_attempts SET failure_reason = 'superseded' "
            "WHERE contact_id = $1 AND attempt > $2 AND state <> 'resolved'",
            attempt["contact_id"], attempt["attempt"],
        )
        await conn.execute(
            "UPDATE campaign_contacts SET status = 'completed', next_attempt_at = NULL, "
            "call_session_id = COALESCE($2, call_session_id) WHERE id = $1 AND status IN ('pending', 'calling', 'failed')",
            attempt["contact_id"], attempt["call_session_id"],
        )
        return
    await conn.execute(
        """
        UPDATE campaign_contacts cc SET
            status = CASE WHEN cc.attempt_count < c.max_attempts THEN 'pending' ELSE $2 END,
            next_attempt_at = CASE WHEN cc.attempt_count < c.max_attempts
                                   THEN $3::timestamptz + make_interval(mins => c.retry_gap_minutes) END
        FROM campaigns c WHERE cc.id = $1 AND c.id = cc.campaign_id AND cc.status = 'calling'
        """,
        attempt["contact_id"], attempt["outcome"], attempt["resolved_at"],
    )
