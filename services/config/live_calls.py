"""
Live Calls Monitoring — poll query and intervention writer.

The KPI aggregate is never LIMITed (so `truncated` is accurate); every
statement filters `tenant_id` in its own WHERE, not via a JOIN.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from libs.tenancy import TenantUnresolved, current_tenant, tenant_conn

from . import audit, db, tenants as tenants_service
from .auth import CurrentUser

LIVE_STAGES = ("ai", "waiting_for_human", "human_connected")
MAX_LIVE_ROWS = 200

# Fail loudly on pool exhaustion rather than queue past the 5s poll.
ACQUIRE_TIMEOUT_S = 5.0

# Masked server-side so the raw MSISDN never leaves the API.
_MASK_PREFIX_LEN = 5
_MASK_SUFFIX_LEN = 4


def _mask_msisdn(number: str | None) -> str | None:
    """'+14155557788' -> '+1415•••7788'; numbers too short to mask are returned as-is."""
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

# Latest-turn snippet: caller_text, else ai_response.
_TRANSCRIPT_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT COALESCE(te.caller_text, te.ai_response) AS snippet
        FROM transcript_entries te
        WHERE te.session_id = c.session_id
        ORDER BY te.turn_number DESC
        LIMIT 1
    ) ts ON true
"""

# Copy of libs/tenancy/session.py's resolver; tenant_conn() has no acquire timeout.
_RESOLVE_SCOPE_SQL = """
    SELECT set_config('app.tenant_id',   t.id::text, true),
           set_config('app.tenant_slug', t.slug,     true)
      FROM tenants t
     WHERE ($1::uuid IS NOT NULL AND t.id = $1::uuid)
        OR ($1::uuid IS NULL AND $2::text IS NOT NULL AND t.slug = $2)
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
    """include_transcript comes from the row role, not the JWT; when False the
    transcript is never fetched. acquire_timeout_s is overridable for tests."""
    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        raise LookupError(f"tenant {tenant_slug!r} not found")
    max_concurrent_calls = tenant["max_concurrent_calls"]

    # current_tenant() is the independent check, and always a UUID here.
    tenant_scope = current_tenant()
    if tenant_scope is None:
        raise TenantUnresolved("could not resolve a tenant for the live-calls poll")

    pool = await db.get_pool()
    async with pool.acquire(timeout=acquire_timeout_s) as conn:
        async with conn.transaction():
            await conn.execute(_RESOLVE_SCOPE_SQL, tenant_scope, None)
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


# No audio join exists yet, so never claim "granted".
INTERVENTION_OUTCOME = "unavailable"

# Same bound as live_call_interventions.session_id's CHECK; caps probe strings.
_AUDITED_SESSION_ID_MAX_LEN = 200


async def request_intervention(
    *, tenant_slug: str, tenant_id: uuid.UUID, session_id: str, action: str,
    user: CurrentUser, ip_address: str | None,
) -> dict[str, Any] | None:
    """None if no live call matches (calls.tenant_id is the slug); else insert + audit in one transaction."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
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


# Denials aggregate into one audit row per (user, tenant) window so probes
# can't flood audit_log; per-tenant so each probed tenant keeps its own trail.
_DENIAL_AUDIT_WINDOW_S = 60.0
_DenialAuditKey = tuple[str, str]  # (user_id, tenant_id)
_denial_audit_windows: dict[_DenialAuditKey, tuple[float, int, int]] = {}  # key -> (window_start, audit_log_id, count)


async def record_denied_intervention(
    *, tenant_id: uuid.UUID, session_id: str, user: CurrentUser, ip_address: str | None,
    detail: str = "not_found_or_out_of_scope",
) -> None:
    """Audit (or aggregate) a refused intervention; never inserts into live_call_interventions."""
    truncated_session_id = session_id[:_AUDITED_SESSION_ID_MAX_LEN]
    now = time.monotonic()
    pool = await db.get_pool()
    key: _DenialAuditKey = (user.id, str(tenant_id))

    window = _denial_audit_windows.get(key)
    if window is not None and now - window[0] < _DENIAL_AUDIT_WINDOW_S:
        window_start, audit_log_id, count = window
        new_count = count + 1
        async with tenant_conn(pool) as conn:
            await conn.execute(
                "UPDATE audit_log SET new_value = jsonb_set(jsonb_set(new_value, "
                "'{count}', to_jsonb($2::int)), '{session_id}', to_jsonb($3::text)), "
                "changed_at = now() WHERE id = $1",
                audit_log_id, new_count, truncated_session_id,
            )
        _denial_audit_windows[key] = (window_start, audit_log_id, new_count)
        return

    new_value = {
        "session_id": truncated_session_id, "requested_action": None,
        "outcome": "denied", "detail": detail, "count": 1,
    }
    async with tenant_conn(pool) as conn:
        audit_log_id = await conn.fetchval(
            "INSERT INTO audit_log (entity_type, entity_id, user_id, user_email, action, new_value, ip_address) "
            "VALUES ('live_call_intervention', $1, $2, $3, 'created', $4::jsonb, $5) RETURNING id",
            tenant_id, user.id, user.email, json.dumps(new_value), ip_address,
        )
    _denial_audit_windows[key] = (now, audit_log_id, 1)
