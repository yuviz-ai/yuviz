"""
Live Calls Monitoring — the poll query and its writer live in one module
(calls.py's own docstring commits it to read-only reporting, and the
intervention writer here would falsify that).

get_live_calls() is two statements on one pool.acquire(): a KPI aggregate
over every live row (never LIMITed, so `truncated` reflects the true count),
then the row list itself, LIMITed to MAX_LIVE_ROWS. Both statements carry
`tenant_id = $1` in their own WHERE — never relying on a JOIN's incidental
exclusion (lesson 12) — and `ended_at IS NULL` is the single definition of
live, reused from calls.py::_status_of.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from . import audit, db, tenants as tenants_service
from .auth import CurrentUser

LIVE_STAGES = ("ai", "waiting_for_human", "human_connected")
MAX_LIVE_ROWS = 200

# The 5s poll's whole connection budget (AC5) — an acquire that can't get a
# connection within this window fails loudly (pool exhaustion surfaces as an
# error the client can retry) rather than queuing indefinitely behind
# whatever else is holding the shared 10-connection pool.
ACQUIRE_TIMEOUT_S = 5.0

# No masking convention exists elsewhere in this repo (the only "mask"
# references are provider_configs.py's secret notes) — this is the one
# definition, applied server-side so the raw MSISDN never leaves the API.
_MASK_PREFIX_LEN = 5
_MASK_SUFFIX_LEN = 4


def _mask_msisdn(number: str | None) -> str | None:
    """'+14155557788' -> '+1415•••7788' (country/area prefix + last 4). A
    number too short to carry both a prefix and a suffix without overlap is
    returned unmasked (there is nothing between them to hide)."""
    if number is None:
        return None
    if len(number) <= _MASK_PREFIX_LEN + _MASK_SUFFIX_LEN:
        return number
    return f"{number[:_MASK_PREFIX_LEN]}•••{number[-_MASK_SUFFIX_LEN:]}"


_KPI_SQL = """
    SELECT
        COUNT(*) AS live_calls,
        COUNT(*) FILTER (WHERE COALESCE(c.live_stage, 'ai') = 'ai') AS ai_only,
        COUNT(*) FILTER (WHERE c.live_stage = 'waiting_for_human') AS waiting_for_human,
        COUNT(*) FILTER (WHERE c.live_stage = 'human_connected') AS human_connected,
        COUNT(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM live_call_interventions i
            WHERE i.tenant_id = c.tenant_id AND i.session_id = c.session_id
        )) AS interventions_pending
    FROM calls c
    WHERE c.tenant_id = $1 AND c.ended_at IS NULL
"""

# Latest-turn snippet: caller_text when the caller most recently spoke,
# falling back to ai_response — either way, "what's happening on this call
# right now". Extension point for a future sentiment indicator (see design's
# Scope decisions): when Conversation begins writing metadata->>'sentiment',
# that's a one-line addition to this SELECT and one column on the row below —
# nothing sentiment-like is read or returned today.
_TRANSCRIPT_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT COALESCE(te.caller_text, te.ai_response) AS snippet
        FROM transcript_entries te
        WHERE te.session_id = c.session_id
        ORDER BY te.turn_number DESC
        LIMIT 1
    ) ts ON true
"""

_ROWS_SQL_TEMPLATE = """
    SELECT
        c.session_id, c.direction, c.caller_number, c.called_number,
        c.started_at, c.live_stage, a.name AS agent_name,
        EXTRACT(EPOCH FROM (NOW() - c.started_at)) * 1000 AS elapsed_ms,
        {transcript_select}
        iv.action AS intervention_action, iv.outcome AS intervention_outcome,
        iv.user_email AS intervention_requested_by_email,
        iv.created_at AS intervention_requested_at
    FROM calls c
    LEFT JOIN agents a ON a.id = c.agent_id AND a.tenant_id = $2
    {transcript_lateral}
    LEFT JOIN LATERAL (
        SELECT action, outcome, user_email, created_at
        FROM live_call_interventions i
        WHERE i.tenant_id = c.tenant_id AND i.session_id = c.session_id
        ORDER BY i.created_at DESC
        LIMIT 1
    ) iv ON true
    WHERE c.tenant_id = $1 AND c.ended_at IS NULL
    ORDER BY c.started_at DESC
    LIMIT {max_rows}
"""


def _row_to_item(row: dict[str, Any], *, include_transcript: bool) -> dict[str, Any]:
    intervention = None
    if row["intervention_action"] is not None:
        intervention = {
            "action": row["intervention_action"],
            "outcome": row["intervention_outcome"],
            "requested_by_email": row["intervention_requested_by_email"],
            "requested_at": row["intervention_requested_at"],
        }
    return {
        "session_id": row["session_id"],
        "agent_name": row["agent_name"],
        "direction": row["direction"],
        "caller_number_masked": _mask_msisdn(row["caller_number"]),
        "called_number_masked": _mask_msisdn(row["called_number"]),
        "live_stage": row["live_stage"] or "ai",
        "started_at": row["started_at"],
        "elapsed_ms": int(row["elapsed_ms"]),
        "transcript_snippet": row["snippet"] if include_transcript else None,
        "transcript_withheld": not include_transcript,
        "intervention": intervention,
    }


async def get_live_calls(
    tenant_slug: str, *, include_transcript: bool, acquire_timeout_s: float = ACQUIRE_TIMEOUT_S,
) -> dict[str, Any]:
    """include_transcript is the caller's decision, made by the router from
    `effective_user.role in deps.TRANSCRIPT_ROLES` — never from the JWT role
    (AC15). When it's False the transcript LATERAL is omitted from the SQL
    entirely, so the text is never fetched, not merely dropped before
    serialization.

    acquire_timeout_s defaults to the shipped ACQUIRE_TIMEOUT_S; the override
    exists only so tests can prove the acquire actually times out (rather
    than hangs) without waiting out the real 5s budget."""
    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        raise LookupError(f"tenant {tenant_slug!r} not found")
    max_concurrent_calls = tenant["max_concurrent_calls"]

    pool = await db.get_pool()
    async with pool.acquire(timeout=acquire_timeout_s) as conn:
        kpi_row = dict(await conn.fetchrow(_KPI_SQL, tenant_slug))
        rows_sql = _ROWS_SQL_TEMPLATE.format(
            transcript_select="ts.snippet," if include_transcript else "NULL AS snippet,",
            transcript_lateral=_TRANSCRIPT_LATERAL if include_transcript else "",
            max_rows=MAX_LIVE_ROWS,
        )
        rows = await conn.fetch(rows_sql, tenant_slug, tenant["id"])

    live_calls = kpi_row["live_calls"]
    utilization_pct = (
        round(live_calls / max_concurrent_calls * 100, 1) if max_concurrent_calls else None
    )

    return {
        "tenant_slug": tenant_slug,
        "generated_at": datetime.now(timezone.utc),
        "refresh_seconds": 5,
        "truncated": live_calls > MAX_LIVE_ROWS,
        "kpis": {
            "live_calls": live_calls,
            "ai_only": kpi_row["ai_only"],
            "waiting_for_human": kpi_row["waiting_for_human"],
            "human_connected": kpi_row["human_connected"],
            "interventions_pending": kpi_row["interventions_pending"],
            "max_concurrent_calls": max_concurrent_calls,
            "utilization_pct": utilization_pct,
        },
        "items": [_row_to_item(dict(row), include_transcript=include_transcript) for row in rows],
    }


# ── POST /live-calls/{session_id}/interventions ──────────────────────────

class InterventionRequest(BaseModel):
    action: Literal["listen", "barge"]
    tenant_slug: str | None = None


# A module constant today — no audio join exists yet (see design's Scope
# decisions), so every successful request is "unavailable", never a
# fabricated "granted"/"human connected". When the telephony join lands,
# this becomes 'granted'/'denied' with no change to this function, the
# table, or the audit shape.
INTERVENTION_OUTCOME = "unavailable"

# live_call_interventions.session_id has a `length(session_id) <= 200` CHECK
# (database/schema.sql) — this path never inserts into that table, but the
# audit row's session_id is truncated to the same bound for consistency and
# so a 300-char probe can't grow an unbounded string into audit_log either.
_AUDITED_SESSION_ID_MAX_LEN = 200


async def request_intervention(
    *, tenant_slug: str, tenant_id: uuid.UUID, session_id: str, action: str,
    user: CurrentUser, ip_address: str | None,
) -> dict[str, Any] | None:
    """Returns None when `SELECT 1 FROM calls WHERE session_id=$1 AND
    tenant_id=$2 AND ended_at IS NULL` finds nothing — tenant_id binds the
    TEXT slug (matching calls.tenant_id's own type), never the tenant UUID,
    so a foreign-tenant or nonexistent session_id can never accidentally
    match (closes security finding #2's type-mismatch predicate). The
    router turns None into the 404 + denial audit (record_denied_intervention
    below). Otherwise one transaction: INSERT live_call_interventions
    (outcome='unavailable') + audit.write_audit(...) — a write_audit failure
    rolls back the INSERT too, so there is never an intervention row with no
    audit trail."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM calls WHERE session_id = $1 AND tenant_id = $2 AND ended_at IS NULL",
                session_id, tenant_slug,
            )
            if exists is None:
                return None

            row = await conn.fetchrow(
                "INSERT INTO live_call_interventions "
                "(tenant_id, session_id, action, outcome, user_id, user_email, ip_address) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                tenant_slug, session_id, action, INTERVENTION_OUTCOME, user.id, user.email, ip_address,
            )
            await audit.write_audit(
                conn,
                entity_type="live_call_intervention",
                entity_id=tenant_id,
                action="created",
                user_id=user.id,
                user_email=user.email,
                new_value={
                    "session_id": session_id,
                    "requested_action": action,
                    "outcome": INTERVENTION_OUTCOME,
                    "detail": None,
                },
                ip_address=ip_address,
            )

    return {
        "action": action,
        "outcome": INTERVENTION_OUTCOME,
        "detail": "audio join not yet available",
        "requested_at": row["created_at"],
    }


# Per-user aggregation window for denial audits (finding #7): a scripted
# prober hammering this route with foreign/nonexistent session ids would
# otherwise grow audit_log by one row per attempt at effectively zero cost.
# Repeated denials from the SAME user inside this window bump one row's
# count/last-seen instead of inserting a new one. Module-level (not
# app.state): T14's own scope is this file, and this bookkeeping — unlike
# fresh_authority's memo — never holds an authorization decision, only a
# denial-audit row id and count, so it carries no risk of granting anything
# the database didn't confirm (lesson 16).
_DENIAL_AUDIT_WINDOW_S = 60.0
_denial_audit_windows: dict[str, tuple[float, int, int]] = {}  # user_id -> (window_start, audit_log_id, count)


async def record_denied_intervention(
    *, tenant_id: uuid.UUID, session_id: str, user: CurrentUser, ip_address: str | None,
    detail: str = "not_found_or_out_of_scope",
) -> None:
    """Writes (or aggregates into) the one audit_log row for a refused
    Listen/Barge request — no live_call_interventions row on this path: that
    table drives in-tenant badges, and letting an arbitrary/out-of-scope
    session_id insert into it would make it a growth vector of its own
    (lesson 30)."""
    truncated_session_id = session_id[:_AUDITED_SESSION_ID_MAX_LEN]
    now = time.monotonic()
    pool = await db.get_pool()

    window = _denial_audit_windows.get(user.id)
    if window is not None and now - window[0] < _DENIAL_AUDIT_WINDOW_S:
        window_start, audit_log_id, count = window
        new_count = count + 1
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE audit_log SET new_value = jsonb_set(new_value, '{count}', to_jsonb($2::int)), "
                "changed_at = now() WHERE id = $1",
                audit_log_id, new_count,
            )
        _denial_audit_windows[user.id] = (window_start, audit_log_id, new_count)
        return

    new_value = {
        "session_id": truncated_session_id, "requested_action": None,
        "outcome": "denied", "detail": detail, "count": 1,
    }
    async with pool.acquire() as conn:
        audit_log_id = await conn.fetchval(
            "INSERT INTO audit_log (entity_type, entity_id, user_id, user_email, action, new_value, ip_address) "
            "VALUES ('live_call_intervention', $1, $2, $3, 'created', $4::jsonb, $5) RETURNING id",
            tenant_id, user.id, user.email, json.dumps(new_value), ip_address,
        )
    _denial_audit_windows[user.id] = (now, audit_log_id, 1)
