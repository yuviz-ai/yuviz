from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from services.config import calls
from services.config.routers.calls import _valid_tz


async def _insert_call(pool, *, tenant_slug, session_id, direction="inbound", ended=False):
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, caller_number, called_number, ended_at) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        session_id, tenant_slug, direction, "+15550100", "+15550199",
        None if not ended else "now()",
    )


async def test_list_calls_scoped_to_tenant_and_decorated(test_tenant, scoped, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=session_id)

    result = await calls.list_calls(test_tenant["slug"])
    assert result["total"] == 1
    call = result["items"][0]
    assert call["session_id"] == session_id
    assert call["direction"] == "inbound"
    assert call["mode"] == "AI"       # derived: inbound -> AI
    assert call["status"] == "live"  # derived: no ended_at yet
    assert call["has_transcript"] is False

    await pool.execute(
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) VALUES ($1, 1, 'hi', 'hello')",
        session_id,
    )
    assert (await calls.list_calls(test_tenant["slug"]))["items"][0]["has_transcript"] is True

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_outbound_call_derives_webrtc_mode(test_tenant, scoped, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=session_id, direction="outbound")

    call = await calls.get_call(session_id)
    assert call["mode"] == "WebRTC"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_ended_call_derives_completed_status(test_tenant, scoped, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, ended_at) VALUES ($1, $2, 'inbound', NOW())",
        session_id, test_tenant["slug"],
    )

    call = await calls.get_call(session_id)
    assert call["status"] == "completed"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_list_calls_filters_by_direction(test_tenant, scoped, pool):
    inbound_id = f"test-call-{uuid.uuid4().hex[:8]}"
    outbound_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=inbound_id, direction="inbound")
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=outbound_id, direction="outbound")

    result = await calls.list_calls(test_tenant["slug"], direction="outbound")
    assert [c["session_id"] for c in result["items"]] == [outbound_id]

    await pool.execute("DELETE FROM calls WHERE session_id IN ($1, $2)", inbound_id, outbound_id)


async def test_list_calls_filters_by_started_at_range(test_tenant, scoped, pool):
    recent_id, old_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(2))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at) "
        "VALUES ($1, $3, 'inbound', NOW() - INTERVAL '1 hour'), ($2, $3, 'inbound', NOW() - INTERVAL '10 days')",
        recent_id, old_id, test_tenant["slug"],
    )
    now = datetime.now(timezone.utc)
    try:
        after = await calls.list_calls(test_tenant["slug"], started_after=now - timedelta(days=1))
        assert [c["session_id"] for c in after["items"]] == [recent_id]
        assert after["total"] == 1

        window = await calls.list_calls(
            test_tenant["slug"], started_after=now - timedelta(days=11), started_before=now - timedelta(days=9),
        )
        assert [c["session_id"] for c in window["items"]] == [old_id]
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id IN ($1, $2)", recent_id, old_id)


async def test_list_calls_filters_by_agent_and_never_crosses_tenants(test_tenant, scoped, pool):
    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        "Agent Filter Cross Tenant", f"test-call-ag-{uuid.uuid4().hex[:8]}",
    )
    mine, sibling, foreign = [
        await pool.fetchval(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, $2, 'A') RETURNING id",
            tenant_id, f"test-agent-{uuid.uuid4().hex[:8]}",
        )
        for tenant_id in (test_tenant["id"], test_tenant["id"], other["id"])
    ]
    ids = [f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, agent_id) VALUES "
        "($1, $4, 'inbound', $6), ($2, $4, 'inbound', $7), ($3, $5, 'inbound', $8)",
        *ids, test_tenant["slug"], other["slug"], mine, sibling, foreign,
    )
    try:
        result = await calls.list_calls(test_tenant["slug"], agent_id=mine)
        assert [c["session_id"] for c in result["items"]] == [ids[0]]
        assert result["total"] == 1
        assert (await calls.list_calls(test_tenant["slug"], agent_id=foreign))["items"] == []
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = ANY($1)", ids)
        await pool.execute("DELETE FROM agents WHERE id = ANY($1)", [mine, sibling, foreign])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def test_get_call_unknown_returns_none():
    assert await calls.get_call("does-not-exist") is None


async def test_get_call_wrong_tenant_returns_none(test_tenant, scoped, pool):
    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        "Call Cross Tenant", f"test-call-x-{uuid.uuid4().hex[:8]}",
    )
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    try:
        await _insert_call(pool, tenant_slug=other["slug"], session_id=session_id)
        assert await calls.get_call(session_id, tenant_slug=test_tenant["slug"]) is None
        assert await calls.get_call(session_id, tenant_slug=other["slug"]) is not None
        assert await calls.get_call(session_id) is not None  # unscoped
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def test_get_transcript_wrong_tenant_returns_empty(test_tenant, scoped, pool):
    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        "Transcript Cross Tenant", f"test-tr-x-{uuid.uuid4().hex[:8]}",
    )
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    try:
        await _insert_call(pool, tenant_slug=other["slug"], session_id=session_id)
        await pool.execute(
            "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
            "VALUES ($1, 1, 'secret', 'reply')",
            session_id,
        )
        assert await calls.get_transcript(session_id, tenant_slug=test_tenant["slug"]) == []
        assert len(await calls.get_transcript(session_id, tenant_slug=other["slug"])) == 1
    finally:
        await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def test_get_latency_stats_computes_percentiles_per_agent_and_engine(test_tenant, scoped, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=session_id)
    await pool.execute(
        "INSERT INTO transcript_entries "
        "(session_id, turn_number, caller_text, ai_response, llm_engine, "
        "stt_latency_ms, llm_latency_ms, tts_latency_ms, latency_ms) VALUES "
        "($1, 1, 'a', 'r', 'GeminiLLM', 100, 300, 150, '{\"voice_to_voice_ms\": 550}'::jsonb), "
        "($1, 2, 'b', 'r', 'GeminiLLM', 120, 500, 180, '{\"voice_to_voice_ms\": 800}'::jsonb), "
        "($1, 3, 'c', 'r', 'GeminiLLM', 110, 9000, 160, '{\"voice_to_voice_ms\": 9270}'::jsonb)",
        session_id,
    )
    # A turn with no voice_to_voice_ms recorded (e.g. cancelled before any
    # audio) must be excluded from the percentile calculation entirely.
    await pool.execute(
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response, llm_engine) "
        "VALUES ($1, 4, 'd', 'r', 'GeminiLLM')",
        session_id,
    )

    stats = await calls.get_latency_stats(test_tenant["slug"], hours=24)

    assert len(stats) == 1
    row = stats[0]
    assert row["llm_engine"] == "GeminiLLM"
    assert row["sample_count"] == 3  # the NULL-latency turn is excluded
    assert row["p50_voice_to_voice_ms"] == 800  # median of 550/800/9270
    assert row["p95_voice_to_voice_ms"] > row["p50_voice_to_voice_ms"]

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_get_latency_stats_respects_hours_window(test_tenant, scoped, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=session_id)
    await pool.execute(
        "INSERT INTO transcript_entries "
        "(session_id, turn_number, caller_text, ai_response, llm_engine, latency_ms, created_at) "
        "VALUES ($1, 1, 'old', 'r', 'GeminiLLM', '{\"voice_to_voice_ms\": 500}'::jsonb, NOW() - INTERVAL '48 hours')",
        session_id,
    )

    stats = await calls.get_latency_stats(test_tenant["slug"], hours=24)
    assert stats == []

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_get_transcript_returns_turns_in_order(test_tenant, scoped, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=session_id)
    await pool.execute(
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
        "VALUES ($1, 2, 'second', 'reply2'), ($1, 1, 'first', 'reply1')",
        session_id,
    )

    turns = await calls.get_transcript(session_id)
    assert [t["turn_number"] for t in turns] == [1, 2]

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_get_dashboard_stats_counts_and_sums_minutes(test_tenant, scoped, pool):
    ok_id, failed_id, live_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(3))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, turn_count, close_reason, ended_at) "
        "VALUES ($1, $2, 'inbound', 120000, 3, 'stream_ended', NOW())",
        ok_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, turn_count, close_reason, ended_at) "
        "VALUES ($1, $2, 'outbound', 60000, 0, 'TRANSFER_FAILED', NOW())",
        failed_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, ended_at) VALUES ($1, $2, 'inbound', NULL)",
        live_id, test_tenant["slug"],
    )

    stats = await calls.get_dashboard_stats(test_tenant["slug"], hours=24)

    assert stats["total_calls"] == 3
    assert stats["total_minutes"] == 3.0  # (120000 + 60000) / 60000
    assert stats["live_calls"] == 1
    assert stats["success_count"] == 1
    assert stats["failed_count"] == 1
    assert stats["inbound_count"] == 2
    assert stats["outbound_count"] == 1

    for sid in (ok_id, failed_id, live_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_dashboard_stats_respects_hours_window(test_tenant, scoped, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at, ended_at) "
        "VALUES ($1, $2, 'inbound', NOW() - INTERVAL '48 hours', NOW() - INTERVAL '48 hours')",
        session_id, test_tenant["slug"],
    )

    stats = await calls.get_dashboard_stats(test_tenant["slug"], hours=24)
    assert stats["total_calls"] == 0
    assert stats["live_calls"] == 0  # ended, and outside the window either way

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_get_usage_trend_groups_by_day(test_tenant, scoped, pool):
    today_id, yesterday_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(2))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, started_at, ended_at) "
        "VALUES ($1, $2, 'inbound', 60000, NOW(), NOW())",
        today_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, started_at, ended_at) "
        "VALUES ($1, $2, 'outbound', 120000, NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day')",
        yesterday_id, test_tenant["slug"],
    )

    trend = await calls.get_usage_trend(test_tenant["slug"], days=30)
    assert len(trend) == 2
    assert sum(row["calls"] for row in trend) == 2
    assert sum(row["minutes"] for row in trend) == 3.0
    assert [(row["inbound"], row["outbound"]) for row in trend] == [(0, 1), (1, 0)]

    for sid in (today_id, yesterday_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_trend_and_activity_report_containment_inputs(test_tenant, scoped, pool):
    ids = [f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, close_reason, started_at, ended_at) VALUES "
        "($1, $4, 'inbound', 'stream_ended', NOW(), NOW()), "
        "($2, $4, 'inbound', 'TRANSFER_FAILED', NOW(), NOW()), "
        "($3, $4, 'inbound', NULL, NOW(), NULL)",
        *ids, test_tenant["slug"],
    )
    try:
        [day] = await calls.get_usage_trend(test_tenant["slug"], days=1)
        assert (day["calls"], day["ended"], day["escalated"]) == (3, 2, 1)
        [hour] = await calls.get_todays_activity(test_tenant["slug"])
        assert (hour["ended"], hour["escalated"]) == (2, 1)
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = ANY($1)", ids)


async def test_get_todays_activity_buckets_by_hour_and_direction(test_tenant, scoped, pool):
    inbound_id, outbound_id, test_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(3))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at) VALUES ($1, $2, 'inbound', NOW())",
        inbound_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at) VALUES ($1, $2, 'outbound', NOW())",
        outbound_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at) VALUES ($1, $2, 'test', NOW())",
        test_id, test_tenant["slug"],
    )

    activity = await calls.get_todays_activity(test_tenant["slug"])
    assert len(activity) == 1  # all calls land in the current hour bucket
    assert activity[0]["inbound"] == 1
    assert activity[0]["outbound"] == 1
    assert activity[0]["web"] == 1

    for sid in (inbound_id, outbound_id, test_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_dashboard_stats_aht_denominator_skips_null_durations(test_tenant, scoped, pool):
    """AHT divides only by calls with a reported duration_ms, not every ended call."""
    timed_id, untimed_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(2))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, close_reason, ended_at) "
        "VALUES ($1, $2, 'inbound', 90000, 'caller_hangup', NOW())",
        timed_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, close_reason, ended_at) "
        "VALUES ($1, $2, 'inbound', 'reconciled_inactive', NOW())",
        untimed_id, test_tenant["slug"],
    )

    stats = await calls.get_dashboard_stats(test_tenant["slug"], hours=24)

    assert stats["ended_count"] == 2
    assert stats["aht_sample_count"] == 1       # not 2
    assert stats["aht_duration_ms"] == 90000

    for sid in (timed_id, untimed_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_dashboard_stats_counts_handoffs_apart_from_escalations(test_tenant, scoped, pool):
    """Handoffs (TRANSFER_SUCCESS) are counted apart from escalations (any transfer attempt)."""
    ok_id, failed_id, plain_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(3))
    for sid, reason in ((ok_id, "TRANSFER_SUCCESS"), (failed_id, "TRANSFER_FAILED"), (plain_id, "caller_hangup")):
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, close_reason, ended_at) "
            "VALUES ($1, $2, 'inbound', 30000, $3, NOW())",
            sid, test_tenant["slug"], reason,
        )

    stats = await calls.get_dashboard_stats(test_tenant["slug"], hours=24)

    assert stats["ended_count"] == 3
    assert stats["handoff_count"] == 1       # TRANSFER_SUCCESS only
    assert stats["escalated_count"] == 2     # both TRANSFER_* rows
    # Containment is computed by the caller as (ended - escalated) / ended.
    assert stats["ended_count"] - stats["escalated_count"] == 1

    for sid in (ok_id, failed_id, plain_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_dashboard_stats_prev_window_is_the_preceding_equal_window(test_tenant, scoped, pool):
    """prev_* powers the trend deltas, so it must cover exactly the window
    immediately before the current one — not all history before it."""
    recent_id, prev_id, ancient_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(3))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, close_reason, started_at, ended_at) "
        "VALUES ($1, $2, 'inbound', 10000, 'caller_hangup', NOW() - INTERVAL '2 hours', NOW() - INTERVAL '2 hours')",
        recent_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, close_reason, started_at, ended_at) "
        "VALUES ($1, $2, 'inbound', 10000, 'caller_hangup', NOW() - INTERVAL '30 hours', NOW() - INTERVAL '30 hours')",
        prev_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, close_reason, started_at, ended_at) "
        "VALUES ($1, $2, 'inbound', 10000, 'caller_hangup', NOW() - INTERVAL '100 hours', NOW() - INTERVAL '100 hours')",
        ancient_id, test_tenant["slug"],
    )

    stats = await calls.get_dashboard_stats(test_tenant["slug"], hours=24)

    assert stats["total_calls"] == 1        # only the 2-hour-old row
    assert stats["prev_total_calls"] == 1   # only the 30-hour-old row, NOT the 100-hour one
    assert stats["prev_ended_count"] == 1

    for sid in (recent_id, prev_id, ancient_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_disposition_mix_groups_ended_calls_only(test_tenant, scoped, pool):
    """Live calls have no disposition yet, so they must not appear — and the
    raw close_reason strings come back unlabelled for the UI to map."""
    a_id, b_id, xfer_id, live_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(4))
    for sid in (a_id, b_id):
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction, close_reason, ended_at) "
            "VALUES ($1, $2, 'inbound', 'caller_hangup', NOW())",
            sid, test_tenant["slug"],
        )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, close_reason, ended_at) "
        "VALUES ($1, $2, 'inbound', 'TRANSFER_SUCCESS', NOW())",
        xfer_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, ended_at) VALUES ($1, $2, 'inbound', NULL)",
        live_id, test_tenant["slug"],
    )

    mix = {row["close_reason"]: row["count"] for row in await calls.get_disposition_mix(test_tenant["slug"], hours=24)}

    assert mix == {"caller_hangup": 2, "TRANSFER_SUCCESS": 1}   # live call absent

    for sid in (a_id, b_id, xfer_id, live_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def _seed_export_calls(pool, tenant_slug):
    ids = [f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, caller_number, called_number, started_at, ended_at, "
        "duration_ms, close_reason, sentiment, turn_count) VALUES "
        "($1, $4, 'inbound', '+15550100', '+15550199', '2026-10-01T10:00:00Z', '2026-10-01T10:01:30Z', "
        "90000, 'TRANSFER_SUCCESS', 'positive', 4), "
        "($2, $4, 'outbound', '=HYPERLINK(\"x\")', '+15550123', '2026-10-02T10:00:00Z', NULL, NULL, NULL, NULL, 0), "
        "($3, $4, 'inbound', '+15550111', '+15550199', '2026-10-03T10:00:00Z', '2026-10-03T10:00:10Z', "
        "10000, 'transport_error', 'frustrated', 1)",
        *ids, tenant_slug,
    )
    return ids


async def _export(base, **overrides):
    from services.config.schemas import CallExport

    # Caller is the first listed tenant, as the router passes it.
    return await calls.export_calls(CallExport(**{**base, **overrides}), tenant_slug=base["tenant_slugs"][0])


async def test_export_csv_applies_filters_and_formats_columns(test_tenant, scoped, pool):
    import csv
    import os

    ids = await _seed_export_calls(pool, test_tenant["slug"])
    spec = {
        "tenant_slugs": [test_tenant["slug"]],
        "columns": ["session_id", "started_at", "caller_number", "duration", "status", "outcome", "sentiment"],
        "timezone": "Asia/Kolkata",
    }
    try:
        path, truncated = await _export(spec, parties="inbound")
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        os.unlink(path)
        assert not truncated
        assert rows[0] == ["Call ID", "Started", "From", "Duration (s)", "Status", "Outcome", "Sentiment"]
        # Newest first, times in the requested zone, phone numbers untouched.
        assert rows[1:] == [
            [ids[2], "2026-10-03 15:30:00", "+15550111", "10", "Completed", "Call dropped", "Frustrated"],
            [ids[0], "2026-10-01 15:30:00", "+15550100", "90", "Completed", "Sent to a person", "Positive"],
        ]

        path, _ = await _export(spec, status="to_person")
        with open(path, encoding="utf-8-sig", newline="") as f:
            assert [r[0] for r in list(csv.reader(f))[1:]] == [ids[0]]
        os.unlink(path)

        path, _ = await _export(spec, status="failed", columns=["session_id"])
        with open(path, encoding="utf-8-sig", newline="") as f:
            assert [r[0] for r in list(csv.reader(f))[1:]] == [ids[2]]
        os.unlink(path)

        path, _ = await _export(spec, status="succeeded", columns=["session_id"])
        with open(path, encoding="utf-8-sig", newline="") as f:
            assert [r[0] for r in list(csv.reader(f))[1:]] == [ids[0]]
        os.unlink(path)

        path, _ = await _export(spec, status="live", columns=["caller_number", "outcome"])
        with open(path, encoding="utf-8-sig", newline="") as f:
            # Formula-looking text is defused; a live call has no outcome yet.
            assert list(csv.reader(f))[1:] == [["'=HYPERLINK(\"x\")", ""]]
        os.unlink(path)

        path, _ = await _export(spec, q="5550111", columns=["session_id"])
        with open(path, encoding="utf-8-sig", newline="") as f:
            assert list(csv.reader(f))[1:] == [[ids[2]]]
        os.unlink(path)
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = ANY($1)", ids)


async def test_trend_and_activity_bucket_in_the_viewers_time_zone(test_tenant, scoped, pool):
    # 00:01 today in India is the previous evening in UTC.
    ids = [f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at) VALUES "
        "($1, $3, 'inbound',  date_trunc('day', NOW() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata' + INTERVAL '1 minute'), "
        "($2, $3, 'outbound', date_trunc('day', NOW() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata' + INTERVAL '1 minute')",
        *ids, test_tenant["slug"],
    )
    try:
        india_today = await pool.fetchval("SELECT (NOW() AT TIME ZONE 'Asia/Kolkata')::date")
        [ist] = await calls.get_usage_trend(test_tenant["slug"], days=2, tz="Asia/Kolkata")
        [utc] = await calls.get_usage_trend(test_tenant["slug"], days=2, tz="UTC")
        assert ist["date"] == india_today and utc["date"] != india_today
        assert (ist["inbound"], ist["outbound"]) == (1, 1)

        activity = await calls.get_todays_activity(test_tenant["slug"], tz="Asia/Kolkata")
        assert [(a["hour"], a["inbound"], a["outbound"]) for a in activity] == [(0, 1, 1)]
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = ANY($1)", ids)


async def test_export_selected_ids_xlsx(test_tenant, scoped, pool):
    import os
    import zipfile

    ids = await _seed_export_calls(pool, test_tenant["slug"])
    try:
        path, _ = await _export(
            {"tenant_slugs": [test_tenant["slug"]], "columns": ["session_id", "duration"]},
            format="xlsx", session_ids=[ids[0], ids[2]], status="live",  # filters ignored for a selection
        )
        with zipfile.ZipFile(path) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
            strings = z.read("xl/sharedStrings.xml").decode() if "xl/sharedStrings.xml" in z.namelist() else sheet
        os.unlink(path)
        assert ids[0] in strings and ids[2] in strings and ids[1] not in strings
        assert "<v>90</v>" in sheet and "<v>10</v>" in sheet  # durations written as numbers
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = ANY($1)", ids)


async def test_export_never_reads_another_tenants_calls(test_tenant, scoped, pool):
    import os

    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        "Export Cross Tenant", f"test-call-ex-{uuid.uuid4().hex[:8]}",
    )
    ids = await _seed_export_calls(pool, other["slug"])
    try:
        path, _ = await _export(
            {"tenant_slugs": [test_tenant["slug"], other["slug"]], "columns": ["session_id"]}, session_ids=ids,
        )
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        os.unlink(path)
        assert content.strip() == "Call ID"
    finally:
        await pool.execute("DELETE FROM calls WHERE session_id = ANY($1)", ids)
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


def test_unknown_time_zone_is_rejected():
    assert _valid_tz("Asia/Kolkata") == "Asia/Kolkata"
    for bad in ("Mars/Olympus", "../etc/passwd", ""):
        with pytest.raises(HTTPException):
            _valid_tz(bad)
