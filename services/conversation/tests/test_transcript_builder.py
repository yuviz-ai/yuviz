from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")

from ..transcript_builder import TranscriptBuilder  # noqa: E402


async def _insert_live_call(pool, session_id: str, *, conv_node: str | None) -> None:
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node) VALUES ($1, 'default', 'inbound', $2)",
        session_id, conv_node,
    )


# ── full begin/turn/end lifecycle ─────────────────────────────────────────
# A regression test for a real bug found live 2026-07-29: _round_or_none()
# was defined at module level in between _record_turn() and _end_call(),
# which silently dedented _end_call() OUT of the class body and nested it
# inside _round_or_none()'s own function scope instead — TranscriptBuilder
# had no _end_call attribute at all. Every existing test exercised
# begin_call/record_turn/reconcile_*/heartbeat individually, never the full
# lifecycle including end_call(), so nothing caught it until a real phone
# call actually hung up and hit the AttributeError live. This test forces
# the full sequence so a similar misplacement can't hide again.
async def test_full_call_lifecycle_begin_turn_end():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-lifecycle-{uuid.uuid4().hex[:8]}"

    builder.begin_call(session_id, "default", "call-1")
    builder.record_turn(session_id, "hello", 0.9, "hi there", False)
    builder.end_call(session_id, "stream_ended")
    await builder._chains[session_id]  # fire-and-forget — wait for the whole chain

    row = await pool.fetchrow(
        "SELECT ended_at, close_reason, turn_count, duration_ms FROM calls WHERE session_id = $1", session_id,
    )
    assert row["ended_at"] is not None
    assert row["close_reason"] == "stream_ended"
    assert row["turn_count"] == 1
    assert row["duration_ms"] is not None

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_reconcile_stale_calls_closes_only_this_node_ids_rows():
    node_id = f"test-host:{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    stale_id = f"test-stale-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, stale_id, conv_node=node_id)

    count = await builder.reconcile_stale_calls()

    assert count >= 1
    row = await pool.fetchrow("SELECT ended_at, close_reason, duration_ms FROM calls WHERE session_id = $1", stale_id)
    assert row["ended_at"] is not None
    assert row["close_reason"] == "reconciled_stale"
    assert row["duration_ms"] is None  # never fabricated, even though ended_at is now set

    await pool.execute("DELETE FROM calls WHERE session_id = $1", stale_id)
    await builder.close()


async def test_reconcile_stale_calls_never_touches_another_instances_live_call():
    """The exact bug this scoping fixes: restarting one Conversation
    Service instance (:50051) must never close out a call genuinely still
    live on a DIFFERENT running instance (:50052) — see project history,
    2026-07-29."""
    this_node = f"test-host:50051-{uuid.uuid4().hex[:8]}"
    other_node = f"test-host:50052-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=this_node)
    pool = builder._pool

    mine_stale = f"test-mine-{uuid.uuid4().hex[:8]}"
    others_live = f"test-others-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, mine_stale, conv_node=this_node)
    await _insert_live_call(pool, others_live, conv_node=other_node)

    await builder.reconcile_stale_calls()

    mine = await pool.fetchrow("SELECT ended_at FROM calls WHERE session_id = $1", mine_stale)
    others = await pool.fetchrow("SELECT ended_at FROM calls WHERE session_id = $1", others_live)
    assert mine["ended_at"] is not None  # my own orphaned call — reconciled
    assert others["ended_at"] is None    # the other instance's live call — untouched

    for sid in (mine_stale, others_live):
        await pool.execute("DELETE FROM calls WHERE session_id = $1", sid)
    await builder.close()


async def test_reconcile_stale_calls_leaves_already_ended_calls_untouched():
    node_id = f"test-host:{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    ended_id = f"test-ended-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, ended_at, close_reason, conv_node) "
        "VALUES ($1, 'default', 'inbound', NOW() - INTERVAL '1 hour', 'stream_ended', $2)",
        ended_id, node_id,
    )

    await builder.reconcile_stale_calls()

    row = await pool.fetchrow("SELECT close_reason FROM calls WHERE session_id = $1", ended_id)
    assert row["close_reason"] == "stream_ended"  # untouched — was never live to begin with

    await pool.execute("DELETE FROM calls WHERE session_id = $1", ended_id)
    await builder.close()


async def test_reconcile_stale_calls_is_a_noop_when_persistence_disabled():
    builder = TranscriptBuilder(pool=None, node_id="test-host:50051")
    assert await builder.reconcile_stale_calls() == 0


async def test_reconcile_stale_calls_is_a_noop_without_a_node_id():
    """No node_id means no safe scope to reconcile within — must skip
    entirely rather than fall back to the old unscoped (and unsafe)
    behavior."""
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])  # node_id defaults to None
    pool = builder._pool
    stale_id = f"test-stale-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, stale_id, conv_node=None)

    count = await builder.reconcile_stale_calls()

    assert count == 0
    row = await pool.fetchrow("SELECT ended_at FROM calls WHERE session_id = $1", stale_id)
    assert row["ended_at"] is None  # untouched

    await pool.execute("DELETE FROM calls WHERE session_id = $1", stale_id)
    await builder.close()


async def test_begin_call_stamps_conv_node():
    node_id = f"test-host:{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    session_id = f"test-begin-{uuid.uuid4().hex[:8]}"

    builder.begin_call(session_id, "default", "call-1")
    await builder._chains[session_id]  # the write is fire-and-forget — wait for it directly

    row = await pool.fetchrow("SELECT conv_node FROM calls WHERE session_id = $1", session_id)
    assert row["conv_node"] == node_id

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


# ── heartbeat / dead-node reconciliation ─────────────────────────────────

async def test_heartbeat_upserts_last_seen_at():
    node_id = f"test-host:{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool

    await builder.heartbeat()
    first = await pool.fetchrow(
        "SELECT last_seen_at FROM conversation_node_heartbeats WHERE node_id = $1", node_id,
    )
    assert first is not None

    await builder.heartbeat()  # second call must UPDATE, not conflict/duplicate
    rows = await pool.fetch("SELECT * FROM conversation_node_heartbeats WHERE node_id = $1", node_id)
    assert len(rows) == 1

    await pool.execute("DELETE FROM conversation_node_heartbeats WHERE node_id = $1", node_id)
    await builder.close()


async def test_heartbeat_is_noop_without_node_id():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])  # node_id=None
    await builder.heartbeat()  # must not raise
    await builder.close()


async def test_reconcile_dead_nodes_closes_calls_for_silent_node():
    dead_node = f"test-dead-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    call_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node) VALUES ($1, 'default', 'inbound', $2)",
        call_id, dead_node,
    )
    # A heartbeat that's well past the staleness threshold — the node once
    # existed but has gone silent, not "never had a heartbeat at all".
    await pool.execute(
        "INSERT INTO conversation_node_heartbeats (node_id, last_seen_at) VALUES ($1, NOW() - INTERVAL '10 minutes')",
        dead_node,
    )

    count = await builder.reconcile_dead_nodes(stale_after_seconds=45)

    assert count >= 1
    row = await pool.fetchrow("SELECT ended_at, close_reason FROM calls WHERE session_id = $1", call_id)
    assert row["ended_at"] is not None
    assert row["close_reason"] == "reconciled_dead_node"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", call_id)
    await pool.execute("DELETE FROM conversation_node_heartbeats WHERE node_id = $1", dead_node)
    await builder.close()


async def test_reconcile_dead_nodes_closes_calls_for_node_with_no_heartbeat_row():
    dead_node = f"test-noheartbeat-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    call_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node) VALUES ($1, 'default', 'inbound', $2)",
        call_id, dead_node,
    )
    # No heartbeat row inserted at all — crashed before its first heartbeat.

    count = await builder.reconcile_dead_nodes(stale_after_seconds=45)

    assert count >= 1
    row = await pool.fetchrow("SELECT ended_at FROM calls WHERE session_id = $1", call_id)
    assert row["ended_at"] is not None

    await pool.execute("DELETE FROM calls WHERE session_id = $1", call_id)
    await builder.close()


async def test_reconcile_dead_nodes_leaves_calls_for_node_with_fresh_heartbeat():
    alive_node = f"test-alive-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    call_id = f"test-call-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node) VALUES ($1, 'default', 'inbound', $2)",
        call_id, alive_node,
    )
    await pool.execute(
        "INSERT INTO conversation_node_heartbeats (node_id, last_seen_at) VALUES ($1, NOW())",
        alive_node,
    )

    await builder.reconcile_dead_nodes(stale_after_seconds=45)

    row = await pool.fetchrow("SELECT ended_at FROM calls WHERE session_id = $1", call_id)
    assert row["ended_at"] is None  # untouched — its node is alive and well

    await pool.execute("DELETE FROM calls WHERE session_id = $1", call_id)
    await pool.execute("DELETE FROM conversation_node_heartbeats WHERE node_id = $1", alive_node)
    await builder.close()


async def test_reconcile_dead_nodes_is_noop_when_persistence_disabled():
    builder = TranscriptBuilder(pool=None)
    assert await builder.reconcile_dead_nodes(stale_after_seconds=45) == 0


async def test_reconcile_inactive_calls_closes_a_call_with_no_recent_activity():
    node_id = f"test-node-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    call_id = f"test-inactive-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node, started_at) "
        "VALUES ($1, 'default', 'inbound', $2, NOW() - INTERVAL '10 minutes')",
        call_id, node_id,
    )

    count = await builder.reconcile_inactive_calls(inactive_after_seconds=45)

    assert count >= 1
    row = await pool.fetchrow("SELECT ended_at, close_reason, duration_ms FROM calls WHERE session_id = $1", call_id)
    assert row["ended_at"] is not None
    assert row["close_reason"] == "reconciled_inactive"
    assert row["duration_ms"] is None  # never fabricated

    await pool.execute("DELETE FROM calls WHERE session_id = $1", call_id)
    await builder.close()


async def test_reconcile_inactive_calls_leaves_a_call_with_recent_transcript_activity():
    node_id = f"test-node-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    call_id = f"test-active-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node, started_at) "
        "VALUES ($1, 'default', 'inbound', $2, NOW() - INTERVAL '10 minutes')",
        call_id, node_id,
    )
    await pool.execute(
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
        "VALUES ($1, 1, 'hello', 'hi there')",
        call_id,
    )

    await builder.reconcile_inactive_calls(inactive_after_seconds=45)

    row = await pool.fetchrow("SELECT ended_at FROM calls WHERE session_id = $1", call_id)
    assert row["ended_at"] is None  # untouched — a real turn just happened

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", call_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", call_id)
    await builder.close()


async def test_reconcile_inactive_calls_leaves_a_freshly_started_call_untouched():
    """started_at is the floor that stops a call from being mistaken for
    stale just because it hasn't had its first turn yet — a call that
    started 2 seconds ago with no transcript_entries is normal, not dead."""
    node_id = f"test-node-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    call_id = f"test-fresh-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node) VALUES ($1, 'default', 'inbound', $2)",
        call_id, node_id,
    )

    await builder.reconcile_inactive_calls(inactive_after_seconds=45)

    row = await pool.fetchrow("SELECT ended_at FROM calls WHERE session_id = $1", call_id)
    assert row["ended_at"] is None  # just started — not stale

    await pool.execute("DELETE FROM calls WHERE session_id = $1", call_id)
    await builder.close()


async def test_reconcile_inactive_calls_leaves_already_ended_calls_untouched():
    node_id = f"test-node-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    call_id = f"test-ended-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node, started_at, ended_at, close_reason) "
        "VALUES ($1, 'default', 'inbound', $2, NOW() - INTERVAL '10 minutes', NOW(), 'stream_ended')",
        call_id, node_id,
    )

    await builder.reconcile_inactive_calls(inactive_after_seconds=45)

    row = await pool.fetchrow("SELECT close_reason FROM calls WHERE session_id = $1", call_id)
    assert row["close_reason"] == "stream_ended"  # untouched, not overwritten

    await pool.execute("DELETE FROM calls WHERE session_id = $1", call_id)
    await builder.close()


async def test_reconcile_inactive_calls_is_a_noop_when_persistence_disabled():
    builder = TranscriptBuilder(pool=None)
    assert await builder.reconcile_inactive_calls(inactive_after_seconds=300) == 0


# ── record_live_stage (Live Calls Monitoring, T18/T19) ───────────────────

async def test_record_live_stage_is_fire_and_forget_and_updates_the_column():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-live-stage-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, session_id, conv_node=None)

    builder.record_live_stage(session_id, "waiting_for_human")
    # The call above returned synchronously with no `await` — proving it
    # didn't block the caller on a real DB round trip — and scheduled a
    # background task we can observe directly, same convention as
    # test_begin_call_stamps_conv_node above.
    assert session_id in builder._chains
    await builder._chains[session_id]

    row = await pool.fetchrow("SELECT live_stage FROM calls WHERE session_id = $1", session_id)
    assert row["live_stage"] == "waiting_for_human"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_record_live_stage_is_noop_when_persistence_disabled():
    builder = TranscriptBuilder(pool=None)
    builder.record_live_stage("some-session", "ai")  # must not raise
    assert builder._chains == {}


async def test_record_live_stage_serializes_per_session_a_later_call_is_never_overtaken(monkeypatch):
    """The design's own risk note: record_live_stage rides the per-session
    _spawn() chain, so an EARLIER hook call's write — even if it happens to
    be slower to actually land on Postgres — can never overtake a LATER
    call's write already applied. Simulated here by artificially delaying
    the first ("waiting_for_human") write; if writes ran unserialized, the
    delayed write finishing last would clobber "human_connected" back to
    "waiting_for_human". T18's own mutation proof (see PR notes) bypasses
    _spawn() to confirm this test actually fails without the chaining."""
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-live-stage-order-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, session_id, conv_node=None)

    original_write = builder._record_live_stage

    async def _slow_for_waiting_for_human(sid, stage):
        if stage == "waiting_for_human":
            await asyncio.sleep(0.2)
        await original_write(sid, stage)

    monkeypatch.setattr(builder, "_record_live_stage", _slow_for_waiting_for_human)

    builder.record_live_stage(session_id, "waiting_for_human")  # on_transfer_initiated
    builder.record_live_stage(session_id, "human_connected")    # on_transfer_completed, right after

    await builder._chains[session_id]
    # Margin past the injected 0.2s delay: proves the FINAL state once both
    # writes have truly landed, not just whichever happened to finish first
    # — an unserialized implementation would otherwise pass this assertion
    # "by luck" (checked before the slow write lands) rather than for real.
    await asyncio.sleep(0.3)

    row = await pool.fetchrow("SELECT live_stage FROM calls WHERE session_id = $1", session_id)
    assert row["live_stage"] == "human_connected"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()
