from __future__ import annotations

import uuid

from services.config import calls


async def _insert_call(pool, *, tenant_slug, session_id, direction="inbound", ended=False):
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, caller_number, called_number, ended_at) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        session_id, tenant_slug, direction, "+15550100", "+15550199",
        None if not ended else "now()",
    )


async def test_list_calls_scoped_to_tenant_and_decorated(test_tenant, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=session_id)

    result = await calls.list_calls(test_tenant["slug"])
    assert result["total"] == 1
    call = result["items"][0]
    assert call["session_id"] == session_id
    assert call["direction"] == "inbound"
    assert call["mode"] == "AI"       # derived: inbound -> AI
    assert call["status"] == "live"  # derived: no ended_at yet

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_outbound_call_derives_webrtc_mode(test_tenant, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=session_id, direction="outbound")

    call = await calls.get_call(session_id)
    assert call["mode"] == "WebRTC"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_ended_call_derives_completed_status(test_tenant, pool):
    session_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, ended_at) VALUES ($1, $2, 'inbound', NOW())",
        session_id, test_tenant["slug"],
    )

    call = await calls.get_call(session_id)
    assert call["status"] == "completed"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_list_calls_filters_by_direction(test_tenant, pool):
    inbound_id = f"test-call-{uuid.uuid4().hex[:8]}"
    outbound_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=inbound_id, direction="inbound")
    await _insert_call(pool, tenant_slug=test_tenant["slug"], session_id=outbound_id, direction="outbound")

    result = await calls.list_calls(test_tenant["slug"], direction="outbound")
    assert [c["session_id"] for c in result["items"]] == [outbound_id]

    await pool.execute("DELETE FROM calls WHERE session_id IN ($1, $2)", inbound_id, outbound_id)


async def test_get_call_unknown_returns_none():
    assert await calls.get_call("does-not-exist") is None


async def test_get_call_wrong_tenant_returns_none(test_tenant, pool):
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


async def test_get_transcript_wrong_tenant_returns_empty(test_tenant, pool):
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


async def test_get_latency_stats_computes_percentiles_per_agent_and_engine(test_tenant, pool):
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


async def test_get_latency_stats_respects_hours_window(test_tenant, pool):
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


async def test_get_transcript_returns_turns_in_order(test_tenant, pool):
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


async def test_get_dashboard_stats_counts_and_sums_minutes(test_tenant, pool):
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
    assert stats["outbound_count"] == 1

    for sid in (ok_id, failed_id, live_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_dashboard_stats_respects_hours_window(test_tenant, pool):
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


async def test_get_usage_trend_groups_by_day(test_tenant, pool):
    today_id, yesterday_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(2))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, started_at, ended_at) "
        "VALUES ($1, $2, 'inbound', 60000, NOW(), NOW())",
        today_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, duration_ms, started_at, ended_at) "
        "VALUES ($1, $2, 'inbound', 120000, NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day')",
        yesterday_id, test_tenant["slug"],
    )

    trend = await calls.get_usage_trend(test_tenant["slug"], days=30)
    assert len(trend) == 2
    assert sum(row["calls"] for row in trend) == 2
    assert sum(row["minutes"] for row in trend) == 3.0

    for sid in (today_id, yesterday_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_todays_activity_buckets_by_hour_and_direction(test_tenant, pool):
    inbound_id, outbound_id = (f"test-call-{uuid.uuid4().hex[:8]}" for _ in range(2))
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at) VALUES ($1, $2, 'inbound', NOW())",
        inbound_id, test_tenant["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, started_at) VALUES ($1, $2, 'outbound', NOW())",
        outbound_id, test_tenant["slug"],
    )

    activity = await calls.get_todays_activity(test_tenant["slug"])
    assert len(activity) == 1  # both calls land in the current hour bucket
    assert activity[0]["inbound"] == 1
    assert activity[0]["outbound"] == 1
    assert activity[0]["web"] == 0

    for sid in (inbound_id, outbound_id):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)


async def test_get_dashboard_stats_aht_denominator_skips_null_durations(test_tenant, pool):
    """AHT must divide by calls that REPORTED a duration, not by every ended
    call — duration_ms is NULL on reconciled/dead-node calls, and counting
    those in the denominator would drag the average toward zero in exact
    proportion to how broken duration reporting is."""
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


async def test_get_dashboard_stats_counts_handoffs_apart_from_escalations(test_tenant, pool):
    """A handoff is a transfer that REACHED a human (TRANSFER_SUCCESS); an
    escalation is any attempt. Containment is the complement of the latter,
    so the two counts must not collapse into one."""
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


async def test_get_dashboard_stats_prev_window_is_the_preceding_equal_window(test_tenant, pool):
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


async def test_get_disposition_mix_groups_ended_calls_only(test_tenant, pool):
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
