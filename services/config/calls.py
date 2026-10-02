"""Read-only reporting over calls / transcript_entries (written by TranscriptBuilder).

Uncached: append-heavy data queried many ways would need per-filter invalidation.
"""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import db


def _status_of(row: dict[str, Any]) -> str:
    """live/completed only: close_reason says why a call ended, not whether it failed."""
    return "live" if row.get("ended_at") is None else "completed"


def _mode_of(row: dict[str, Any]) -> str:
    """Display label derived from `direction`."""
    return "AI" if row.get("direction") == "inbound" else "WebRTC"


# JSONB columns decoded so API consumers get objects, not JSON strings.
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
    """tenant_slug, not tenant_id: calls.tenant_id is a TEXT slug, not a UUID FK."""
    pool = await db.get_pool()
    where = ["c.tenant_id = $1"]
    params: list[Any] = [tenant_slug]
    if direction is not None:
        params.append(direction)
        where.append(f"c.direction = ${len(params)}")
    where_clause = " AND ".join(where)

    async with tenant_conn(pool) as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM calls c WHERE {where_clause}", *params)

        params.extend([limit, offset])
        rows = await conn.fetch(
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
        async with platform_conn(pool, reason="calls-platform-read") as conn:
            row = await conn.fetchrow(
                "SELECT c.*, a.name AS agent_name FROM calls c "
                "LEFT JOIN agents a ON a.id = c.agent_id "
                "WHERE c.session_id = $1",
                session_id,
            )
    else:
        async with tenant_conn(pool) as conn:
            row = await conn.fetchrow(
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
        async with platform_conn(pool, reason="calls-platform-read") as conn:
            rows = await conn.fetch(
                "SELECT * FROM transcript_entries WHERE session_id = $1 ORDER BY turn_number",
                session_id,
            )
    else:
        async with tenant_conn(pool) as conn:
            rows = await conn.fetch(
                "SELECT te.* FROM transcript_entries te "
                "JOIN calls c ON c.session_id = te.session_id "
                "WHERE te.session_id = $1 AND c.tenant_id = $2 "
                "ORDER BY te.turn_number",
                session_id, tenant_slug,
            )
    return [dict(row) for row in rows]


async def get_dashboard_stats(tenant_slug: str, *, hours: int = 24 * 30) -> dict[str, Any]:
    """Headline Dashboard numbers for one tenant over `hours`.

    "success" = ended with turns and no TRANSFER_FAILED; live_calls ignores the window."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
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
            ) AS outbound_count,

            -- ── Headline-tile aggregates (Containment / AHT / Handoffs) ──
            -- Every one of these is returned as raw numerator + denominator
            -- rather than a finished percentage or average, because the
            -- Dashboard sums them across ALL tenants (listAllDashboardStats)
            -- and an average of per-tenant averages is not the average — a
            -- tenant with 3 calls would weigh the same as one with 30,000.
            COUNT(*) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour') AND ended_at IS NOT NULL
            ) AS ended_count,
            -- AHT's denominator is NOT ended_count: duration_ms is NULL on
            -- calls that ended without the Gateway ever reporting a duration
            -- (reconciled dead nodes, and every inbound/outbound row in the
            -- current dev DB). Dividing by ended_count would silently drag
            -- the average toward zero in exact proportion to how broken
            -- duration reporting is, which is the opposite of informative.
            COUNT(*) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour')
                  AND ended_at IS NOT NULL AND duration_ms IS NOT NULL
            ) AS aht_sample_count,
            COALESCE(SUM(duration_ms) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour')
                  AND ended_at IS NOT NULL AND duration_ms IS NOT NULL
            ), 0) AS aht_duration_ms,
            -- A handoff is TRANSFER_SUCCESS only — a human actually took the
            -- call. TRANSFER_FAILED/TRANSFER_TIMEOUT never reached anyone, so
            -- counting them here would overstate how much load agents take.
            COUNT(*) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour')
                  AND close_reason = 'TRANSFER_SUCCESS'
            ) AS handoff_count,
            -- Containment's complement is every ESCALATION ATTEMPT, not just
            -- the successful ones: a call the AI tried to hand off was not
            -- resolved without a human, whether or not the transfer landed.
            COUNT(*) FILTER (
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour')
                  AND close_reason LIKE 'TRANSFER%'
            ) AS escalated_count,

            -- ── Same five, one window earlier, for the trend deltas ──
            -- Compared against the immediately preceding window of equal
            -- length, so "+8.2%" always means "vs the previous `hours`",
            -- never an all-time or calendar-period comparison.
            COUNT(*) FILTER (WHERE started_at >= NOW() - (2 * $2 * INTERVAL '1 hour')
                               AND started_at <  NOW() - ($2 * INTERVAL '1 hour')) AS prev_total_calls,
            COUNT(*) FILTER (WHERE started_at >= NOW() - (2 * $2 * INTERVAL '1 hour')
                               AND started_at <  NOW() - ($2 * INTERVAL '1 hour')
                               AND ended_at IS NOT NULL) AS prev_ended_count,
            COUNT(*) FILTER (WHERE started_at >= NOW() - (2 * $2 * INTERVAL '1 hour')
                               AND started_at <  NOW() - ($2 * INTERVAL '1 hour')
                               AND ended_at IS NOT NULL AND duration_ms IS NOT NULL) AS prev_aht_sample_count,
            COALESCE(SUM(duration_ms) FILTER (
                WHERE started_at >= NOW() - (2 * $2 * INTERVAL '1 hour')
                  AND started_at <  NOW() - ($2 * INTERVAL '1 hour')
                  AND ended_at IS NOT NULL AND duration_ms IS NOT NULL
            ), 0) AS prev_aht_duration_ms,
            COUNT(*) FILTER (WHERE started_at >= NOW() - (2 * $2 * INTERVAL '1 hour')
                               AND started_at <  NOW() - ($2 * INTERVAL '1 hour')
                               AND close_reason = 'TRANSFER_SUCCESS') AS prev_handoff_count,
            COUNT(*) FILTER (WHERE started_at >= NOW() - (2 * $2 * INTERVAL '1 hour')
                               AND started_at <  NOW() - ($2 * INTERVAL '1 hour')
                               AND close_reason LIKE 'TRANSFER%') AS prev_escalated_count
        FROM calls WHERE tenant_id = $1
        """,
            tenant_slug, hours,
        )
    d = dict(row)
    d["total_minutes"] = round(d.pop("total_duration_ms") / 60000, 2)
    return d


async def get_disposition_mix(tenant_slug: str, *, hours: int = 24 * 30) -> list[dict[str, Any]]:
    """Ended calls in the window by raw close_reason; the caller labels them."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(close_reason, 'unknown') AS close_reason, COUNT(*) AS count
            FROM calls
            WHERE tenant_id = $1
              AND started_at >= NOW() - ($2 * INTERVAL '1 hour')
              AND ended_at IS NOT NULL
            GROUP BY COALESCE(close_reason, 'unknown')
            ORDER BY count DESC
            """,
            tenant_slug, hours,
        )
    return [dict(row) for row in rows]


async def get_usage_trend(tenant_slug: str, *, days: int = 30) -> list[dict[str, Any]]:
    """Calls + minutes per calendar day, for the Usage Trends chart."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
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
    """Today's calls by hour and direction; 'web' is always 0 (not a persisted channel)."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
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
    """Per-agent, per-LLM-engine latency percentiles; turns without voice_to_voice_ms are excluded."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
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
