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

from datetime import datetime, timezone
from typing import Any

from . import db, tenants as tenants_service

LIVE_STAGES = ("ai", "waiting_for_human", "human_connected")
MAX_LIVE_ROWS = 200

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


async def get_live_calls(tenant_slug: str, *, include_transcript: bool) -> dict[str, Any]:
    """include_transcript is the caller's decision, made by the router from
    `effective_user.role in deps.TRANSCRIPT_ROLES` — never from the JWT role
    (AC15). When it's False the transcript LATERAL is omitted from the SQL
    entirely, so the text is never fetched, not merely dropped before
    serialization."""
    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        raise LookupError(f"tenant {tenant_slug!r} not found")
    max_concurrent_calls = tenant["max_concurrent_calls"]

    pool = await db.get_pool()
    async with pool.acquire() as conn:
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
