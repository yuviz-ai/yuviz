"""Read-only reporting over calls / transcript_entries (written by TranscriptBuilder).

Uncached: append-heavy data queried many ways would need per-filter invalidation.
"""

from __future__ import annotations

import asyncio
import csv
import re
import tempfile
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import xlsxwriter

from libs.tenancy import platform_conn, tenant_conn

from . import db

if TYPE_CHECKING:
    from .schemas import CallExport


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
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    agent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """tenant_slug, not tenant_id: calls.tenant_id is a TEXT slug, not a UUID FK."""
    pool = await db.get_pool()
    where = ["c.tenant_id = $1"]
    params: list[Any] = [tenant_slug]
    if agent_id is not None:
        params.append(agent_id)
        where.append(f"c.agent_id = ${len(params)}")
    if direction is not None:
        params.append(direction)
        where.append(f"c.direction = ${len(params)}")
    if started_after is not None:
        params.append(started_after)
        where.append(f"c.started_at >= ${len(params)}")
    if started_before is not None:
        params.append(started_before)
        where.append(f"c.started_at <= ${len(params)}")
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
                WHERE started_at >= NOW() - ($2 * INTERVAL '1 hour') AND direction = 'inbound'
            ) AS inbound_count,
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
    """Calls, minutes and containment inputs (ended/escalated) per calendar day."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            """
            SELECT
                date_trunc('day', started_at)::date AS date,
                COUNT(*) AS calls,
                COUNT(*) FILTER (WHERE direction = 'inbound') AS inbound,
                COUNT(*) FILTER (WHERE direction = 'outbound') AS outbound,
                ROUND(COALESCE(SUM(duration_ms), 0) / 60000.0, 2) AS minutes,
                COUNT(*) FILTER (WHERE ended_at IS NOT NULL) AS ended,
                COUNT(*) FILTER (WHERE close_reason LIKE 'TRANSFER%') AS escalated
            FROM calls
            WHERE tenant_id = $1 AND started_at >= NOW() - ($2 * INTERVAL '1 day')
            GROUP BY date_trunc('day', started_at)
            ORDER BY date
            """,
            tenant_slug, days,
        )
    return [dict(row) for row in rows]


async def get_todays_activity(tenant_slug: str) -> list[dict[str, Any]]:
    """Today's calls by hour and direction; 'web' is browser test sessions (direction 'test')."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            """
            SELECT
                EXTRACT(HOUR FROM started_at)::int AS hour,
                COUNT(*) FILTER (WHERE direction = 'inbound') AS inbound,
                COUNT(*) FILTER (WHERE direction = 'outbound') AS outbound,
                COUNT(*) FILTER (WHERE direction = 'test') AS web,
                COUNT(*) FILTER (WHERE ended_at IS NOT NULL) AS ended,
                COUNT(*) FILTER (WHERE close_reason LIKE 'TRANSFER%') AS escalated
            FROM calls
            WHERE tenant_id = $1 AND started_at >= date_trunc('day', NOW())
            GROUP BY hour ORDER BY hour
            """,
            tenant_slug,
        )
    return [dict(r) for r in rows]


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


# ── Export ─────────────────────────────────────────────────────────────────

EXPORT_MAX_ROWS = 50_000

# Mirrors admin-ui/lib/callOutcome.ts.
_OUTCOME_REASONS = {
    "done": ("caller_hangup", "stream_ended", "session_destroyed"),
    "to_person": ("TRANSFER_SUCCESS",),
    "transfer_failed": ("TRANSFER_FAILED", "TRANSFER_TIMEOUT"),
    "dropped": (
        "transport_error", "close_timeout", "reconciled_inactive", "reconciled_stale", "reconciled_dead_node",
    ),
}
_OUTCOME_LABELS = {
    "done": "Ended normally", "to_person": "Sent to a person",
    "transfer_failed": "Transfer failed", "dropped": "Call dropped",
}
_REASON_OUTCOME = {reason: key for key, reasons in _OUTCOME_REASONS.items() for reason in reasons}
_DIRECTION_LABELS = {"inbound": "Inbound", "outbound": "Outbound", "test": "Test (browser)"}

_EXPORT_HEADERS = {
    "session_id": "Call ID", "started_at": "Started", "ended_at": "Ended", "account": "Account",
    "direction": "Direction", "caller_number": "From", "called_number": "To", "agent": "Agent",
    "duration": "Duration (s)", "status": "Status", "outcome": "Outcome", "close_reason": "Close reason",
    "sentiment": "Sentiment", "sentiment_reason": "Sentiment reason", "turns": "Turns",
    "disposition": "Disposition", "languages": "Languages",
}
_EXPORT_WIDTHS = {"session_id": 38, "started_at": 20, "ended_at": 20, "sentiment_reason": 48, "disposition": 24}

_SEARCH_COLUMNS = (
    "t.name", "a.name", "c.caller_number", "c.called_number", "c.disposition", "c.sentiment_reason", "c.session_id",
)


def _export_where(spec: CallExport, tenant_slugs: list[str]) -> tuple[str, list[Any]]:
    where = ["c.tenant_id = ANY($1)"]
    params: list[Any] = [tenant_slugs]

    def add(clause: str, value: Any) -> None:
        params.append(value)
        where.append(clause.format(p=f"${len(params)}"))

    if spec.session_ids is not None:
        add("c.session_id = ANY({p})", spec.session_ids)
        return " AND ".join(where), params

    if spec.started_after is not None:
        add("c.started_at >= {p}", spec.started_after)
    if spec.started_before is not None:
        add("c.started_at <= {p}", spec.started_before)
    if spec.q and spec.q.strip():
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", spec.q.strip()) + "%"
        add("(" + " OR ".join(f"{col} ILIKE {{p}}" for col in _SEARCH_COLUMNS) + ")", pattern)
    if spec.parties in ("inbound", "AI"):
        where.append("c.direction = 'inbound'")
    elif spec.parties == "outbound":
        where.append("c.direction = 'outbound'")
    elif spec.parties == "WebRTC":
        where.append("c.direction <> 'inbound'")
    if spec.agent == "__none":
        where.append("a.name IS NULL")
    elif spec.agent is not None:
        add("a.name = {p}", spec.agent)
    where.extend({
        "short": ["c.duration_ms < 30000"],
        "mid": ["c.duration_ms >= 30000", "c.duration_ms < 120000"],
        "long": ["c.duration_ms >= 120000"],
        "none": ["c.duration_ms IS NULL"],
    }.get(spec.duration or "", []))
    if spec.sentiment == "unscored":
        where.append("c.sentiment IS NULL")
    elif spec.sentiment is not None:
        add("c.sentiment = {p}", spec.sentiment)
    if spec.status == "live":
        where.append("c.ended_at IS NULL")
    elif spec.status == "completed":
        where.append("c.ended_at IS NOT NULL")
    elif spec.status is not None:
        where.append("c.ended_at IS NOT NULL")
        add("c.close_reason = ANY({p})", list(_OUTCOME_REASONS[spec.status]))
    where.extend({
        "0": ["COALESCE(c.turn_count, 0) = 0"],
        "few": ["c.turn_count BETWEEN 1 AND 5"],
        "many": ["c.turn_count > 5"],
    }.get(spec.turns or "", []))
    return " AND ".join(where), params


def _export_value(column: str, row: dict[str, Any], tz: ZoneInfo) -> Any:
    match column:
        case "started_at" | "ended_at":
            ts = row[column]
            return ts.astimezone(tz).replace(tzinfo=None, microsecond=0) if ts else None
        case "account":
            return row["tenant_name"] or row["tenant_slug"]
        case "direction":
            return _DIRECTION_LABELS.get(row["direction"], row["direction"])
        case "agent":
            return row["agent_name"]
        case "duration":
            return round(row["duration_ms"] / 1000) if row["duration_ms"] is not None else None
        case "status":
            return "Live" if row["ended_at"] is None else "Completed"
        case "outcome":
            if row["ended_at"] is None:
                return None
            return _OUTCOME_LABELS.get(_REASON_OUTCOME.get(row["close_reason"], ""), "Not recorded")
        case "sentiment":
            return row["sentiment"].capitalize() if row["sentiment"] else None
        case "turns":
            return row["turn_count"] or 0
        case "languages":
            return ", ".join(row["detected_languages"] or []) or None
        case _:
            return row[column]


_PHONE_LIKE = re.compile(r"^[+\-]?[\d\s()\-]+$")


def _csv_safe(value: Any) -> Any:
    """Defuse spreadsheet formula injection without mangling "+1555…" phone numbers."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") and not _PHONE_LIKE.match(value):
        return "'" + value
    return value


def _write_csv(path: str, columns: list[str], rows: list[list[Any]]) -> None:
    # utf-8-sig so Excel detects UTF-8 when opening the CSV directly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([_EXPORT_HEADERS[c] for c in columns])
        for row in rows:
            writer.writerow([
                "" if v is None else v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else _csv_safe(v)
                for v in row
            ])


def _write_xlsx(path: str, columns: list[str], rows: list[list[Any]]) -> None:
    workbook = xlsxwriter.Workbook(path, {"constant_memory": True})
    sheet = workbook.add_worksheet("Calls")
    header = workbook.add_format({"bold": True, "bg_color": "#EFEAE0", "border": 1})
    when = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    for i, column in enumerate(columns):
        sheet.set_column(i, i, _EXPORT_WIDTHS.get(column, 16))
        sheet.write_string(0, i, _EXPORT_HEADERS[column], header)
    for r, row in enumerate(rows, start=1):
        for i, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, datetime):
                sheet.write_datetime(r, i, value, when)
            elif isinstance(value, (int, float)):
                sheet.write_number(r, i, value)
            else:
                sheet.write_string(r, i, str(value))
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, len(rows), len(columns) - 1)
    workbook.close()


async def export_calls(spec: CallExport, *, tenant_slug: str | None) -> tuple[str, bool]:
    """Write matching calls (newest first) to a temp file; returns (path, truncated). Caller deletes the file.

    tenant_slug=None is platform-scoped (spec.tenant_slugs used as given); otherwise only that tenant is read.
    """
    tz = ZoneInfo(spec.timezone)
    where_clause, params = _export_where(spec, spec.tenant_slugs if tenant_slug is None else [tenant_slug])
    params.append(EXPORT_MAX_ROWS + 1)
    sql = (
        "SELECT c.session_id, c.tenant_id AS tenant_slug, t.name AS tenant_name, c.started_at, c.ended_at, "
        "c.direction, c.caller_number, c.called_number, a.name AS agent_name, c.duration_ms, c.close_reason, "
        "c.sentiment, c.sentiment_reason, c.turn_count, c.disposition, c.detected_languages "
        "FROM calls c LEFT JOIN agents a ON a.id = c.agent_id LEFT JOIN tenants t ON t.slug = c.tenant_id "
        f"WHERE {where_clause} ORDER BY c.started_at DESC LIMIT ${len(params)}"
    )
    pool = await db.get_pool()
    if tenant_slug is None:
        async with platform_conn(pool, reason="calls-platform-export") as conn:
            records = await conn.fetch(sql, *params)
    else:
        async with tenant_conn(pool) as conn:
            records = await conn.fetch(sql, *params)

    truncated = len(records) > EXPORT_MAX_ROWS
    rows = [
        [_export_value(column, row, tz) for column in spec.columns]
        for row in map(dict, records[:EXPORT_MAX_ROWS])
    ]
    with tempfile.NamedTemporaryFile(prefix="calls-export-", suffix=f".{spec.format}", delete=False) as f:
        path = f.name
    writer = _write_xlsx if spec.format == "xlsx" else _write_csv
    await asyncio.to_thread(writer, path, list(spec.columns), rows)
    return path, truncated
