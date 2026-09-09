"""
Calls — read-only reporting over the calls / transcript_entries tables
(database/schema.sql). Written by TranscriptBuilder (services/conversation),
never by this module — Config Service only reads call history for the Admin
UI, it never mutates it.

Deliberately NOT cache-aside like tenants.py/agents.py/etc.: call history is
append-heavy reporting data (new rows constantly, list queries filtered/
paginated many different ways), not a small set of hot-path config lookups —
a cache here would either serve stale "recent calls" or need per-filter
invalidation for no real benefit.
"""

from __future__ import annotations

from typing import Any

from . import db


def _status_of(row: dict[str, Any]) -> str:
    """Derived, not stored: close_reason distinguishes *why* a call ended
    (stream_ended, goodbye_timeout, TRANSFER_SUCCESS/FAILED/TIMEOUT — see
    ConversationSession.close()), not whether it failed operationally, so
    the only honest status split is still whether the call is in
    progress."""
    return "live" if row.get("ended_at") is None else "completed"


def _mode_of(row: dict[str, Any]) -> str:
    """Display label, not stored: inbound calls are answered by the AI
    directly; outbound calls (not yet built — see project memory, no
    campaign/dialer code exists) are placed via WebRTC. Derived from
    `direction` so there's one source of truth, not two columns that can
    drift apart."""
    return "AI" if row.get("direction") == "inbound" else "WebRTC"


# Written as JSONB by TranscriptBuilder.record_workflow_outcome(). Decode so
# API consumers get objects/lists, not JSON strings.
_JSON_COLUMNS = ("nodes_visited", "extracted_variables")


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    row["status"] = _status_of(row)
    row["mode"] = _mode_of(row)
    for column in _JSON_COLUMNS:
        if column in row:
            row[column] = db.json_col(row[column])
    return row


async def list_calls(
    tenant_slug: str,
    *,
    limit: int = 50,
    offset: int = 0,
    direction: str | None = None,
) -> dict[str, Any]:
    """tenant_slug, not tenant_id: calls.tenant_id is a TEXT slug reference
    (matching tenants.slug), not a UUID FK — see schema.sql's note on why."""
    pool = await db.get_pool()
    where = ["c.tenant_id = $1"]
    params: list[Any] = [tenant_slug]
    if direction is not None:
        params.append(direction)
        where.append(f"c.direction = ${len(params)}")
    where_clause = " AND ".join(where)

    total = await pool.fetchval(f"SELECT COUNT(*) FROM calls c WHERE {where_clause}", *params)

    params.extend([limit, offset])
    rows = await pool.fetch(
        f"SELECT c.*, a.name AS agent_name FROM calls c "
        f"LEFT JOIN agents a ON a.id = c.agent_id "
        f"WHERE {where_clause} "
        f"ORDER BY c.started_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
        *params,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_decorate(dict(row)) for row in rows],
    }


async def get_call(
    session_id: str,
    *,
    tenant_slug: str | None = None,
) -> dict[str, Any] | None:
    """tenant_slug=None is platform-scoped (superadmin / service account)."""
    pool = await db.get_pool()
    if tenant_slug is None:
        row = await pool.fetchrow(
            "SELECT c.*, a.name AS agent_name FROM calls c "
            "LEFT JOIN agents a ON a.id = c.agent_id "
            "WHERE c.session_id = $1",
            session_id,
        )
    else:
        row = await pool.fetchrow(
            "SELECT c.*, a.name AS agent_name FROM calls c "
            "LEFT JOIN agents a ON a.id = c.agent_id "
            "WHERE c.session_id = $1 AND c.tenant_id = $2",
            session_id, tenant_slug,
        )
    return _decorate(dict(row)) if row is not None else None


async def get_transcript(
    session_id: str,
    *,
    tenant_slug: str | None = None,
) -> list[dict[str, Any]]:
    """tenant_slug scopes via the owning calls row (same predicate as get_call)."""
    pool = await db.get_pool()
    if tenant_slug is None:
        rows = await pool.fetch(
            "SELECT * FROM transcript_entries WHERE session_id = $1 ORDER BY turn_number",
            session_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT te.* FROM transcript_entries te "
            "JOIN calls c ON c.session_id = te.session_id "
            "WHERE te.session_id = $1 AND c.tenant_id = $2 "
            "ORDER BY te.turn_number",
            session_id, tenant_slug,
        )
    return [dict(row) for row in rows]


async def get_dashboard_stats(tenant_slug: str, *, hours: int = 24 * 30) -> dict[str, Any]:
    """Headline Dashboard numbers for one tenant, windowed by `hours` (default
    30 days, matching Usage Trends' own default window below).

    success/failed is a deliberately narrow, honest proxy — this platform has
    no business-outcome tracking (no "booking succeeded" flag), so "success"
    here means "the call actually held a conversation" (turn_count > 0) and
    wasn't a hard transfer failure, not "the caller got what they wanted".
    Only counted among ENDED calls — a live call with 0 turns so far just
    hasn't had one yet, that's not the same as having failed.
    live_calls (ended_at IS NULL) ignores the hours window on purpose — a
    call in progress right now is live regardless of when it started."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour')) AS total_calls,
            COALESCE(SUM(duration_ms) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour')
            ), 0) AS total_duration_ms,
            COUNT(*) FILTER (WHERE ended_at IS NULL) AS live_calls,
            COUNT(*) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour') AND ended_at IS NOT NULL
                  AND turn_count > 0 AND close_reason IS DISTINCT FROM 'TRANSFER_FAILED'
            ) AS success_count,
            COUNT(*) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour') AND ended_at IS NOT NULL
                  AND (turn_count = 0 OR close_reason = 'TRANSFER_FAILED')
            ) AS failed_count,
            COUNT(*) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour') AND direction = 'outbound'
            ) AS outbound_count
        FROM calls WHERE tenant_id = $1
        """,
        tenant_slug, hours,
    )
    d = dict(row)
    d["total_minutes"] = round(d.pop("total_duration_ms") / 60000, 2)
    return d


async def get_usage_trend(tenant_slug: str, *, days: int = 30) -> list[dict[str, Any]]:
    """Calls + minutes per calendar day, for the Usage Trends chart."""
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        SELECT
            date_trunc('day', started_at)::date AS date,
            COUNT(*) AS calls,
            ROUND(COALESCE(SUM(duration_ms), 0) / 60000.0, 2) AS minutes
        FROM calls
        WHERE tenant_id = $1 AND started_at >= NOW() - ($2 * INTERVAL '1 day')
        GROUP BY date_trunc('day', started_at)
        ORDER BY date
        """,
        tenant_slug, days,
    )
    return [dict(row) for row in rows]


async def get_todays_activity(tenant_slug: str) -> list[dict[str, Any]]:
    """Today's calls bucketed by hour and channel — direction distinguishes
    inbound/outbound; 'web' isn't a real channel this platform has (no
    browser-only calls are persisted as their own direction), so it's
    always 0 here rather than fabricated — see _mode_of()'s own note that
    "WebRTC" is just outbound's display label today, not a separate
    channel."""
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        SELECT
            EXTRACT(HOUR FROM started_at)::int AS hour,
            COUNT(*) FILTER (WHERE direction = 'inbound') AS inbound,
            COUNT(*) FILTER (WHERE direction = 'outbound') AS outbound
        FROM calls
        WHERE tenant_id = $1 AND started_at >= date_trunc('day', NOW())
        GROUP BY hour ORDER BY hour
        """,
        tenant_slug,
    )
    return [{"hour": r["hour"], "inbound": r["inbound"], "outbound": r["outbound"], "web": 0} for r in rows]


async def get_latency_stats(tenant_slug: str, *, hours: int = 24) -> list[dict[str, Any]]:
    """Per-agent, per-LLM-engine voice-to-voice latency percentiles — the
    dashboard's whole reason for existing (see project history: repeated
    manual "does Gemini feel faster than Groq" judgment calls this session,
    now backed by real numbers instead). voice_to_voice_ms only exists
    inside latency_ms JSONB (see transcript_builder.py's TurnLatency); the
    per-stage numbers have their own plain columns too, populated
    alongside it. Turns with no voice_to_voice_ms (STT produced nothing,
    cancelled before any audio) are excluded — including them would silently
    pull percentiles toward zero-ish nonsense rather than reflect real
    responses.
    """
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        SELECT
            c.agent_id,
            a.name AS agent_name,
            te.llm_engine,
            COUNT(*) AS sample_count,
            PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY (te.latency_ms->>'voice_to_voice_ms')::float
            ) AS p50_voice_to_voice_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP (
                ORDER BY (te.latency_ms->>'voice_to_voice_ms')::float
            ) AS p95_voice_to_voice_ms,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY te.stt_latency_ms) AS p50_stt_ms,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY te.llm_latency_ms) AS p50_llm_ms,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY te.tts_latency_ms) AS p50_tts_ms
        FROM transcript_entries te
        JOIN calls c ON c.session_id = te.session_id
        LEFT JOIN agents a ON a.id = c.agent_id
        WHERE c.tenant_id = $1
          AND te.created_at >= NOW() - ($2 * INTERVAL '1 hour')
          AND te.latency_ms->>'voice_to_voice_ms' IS NOT NULL
        GROUP BY c.agent_id, a.name, te.llm_engine
        ORDER BY c.agent_id, te.llm_engine
        """,
        tenant_slug, hours,
    )
    return [dict(row) for row in rows]
