from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import asyncpg
import pytest

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")

from .. import transcript_builder  # noqa: E402
from ..transcript_builder import TranscriptBuilder  # noqa: E402


async def _insert_live_call(pool, session_id: str, *, conv_node: str | None) -> None:
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, conv_node) VALUES ($1, 'default', 'inbound', $2)",
        session_id, conv_node,
    )


# ── full begin/turn/end lifecycle ─────────────────────────────────────────
# Exercises begin/turn/end together so a method silently falling out of the class body is caught.
async def test_full_call_lifecycle_begin_turn_end():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-lifecycle-{uuid.uuid4().hex[:8]}"

    builder.begin_call(session_id, "default", "call-1")
    builder.record_turn(session_id, "hello", 0.9, "hi there", False)
    builder.end_call(session_id, "stream_ended", score_sentiment=False)
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
    """Reconciling one instance never closes a call still live on a different instance."""
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
    """Without a node_id there's no safe scope, so reconcile skips entirely."""
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


async def test_heartbeat_raises_when_pool_is_closed():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=f"test-host:{uuid.uuid4().hex[:8]}")
    await builder.close()
    with pytest.raises(asyncpg.InterfaceError):
        await builder.heartbeat()


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
    """started_at keeps a just-started call with no turns from being treated as stale."""
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


# ── RLS: sweeps hold no open transaction between iterations (T52) ────────
# Each sweep's platform_conn() transaction must close before the method returns.
async def test_reconcile_sweeps_leave_no_open_transaction_between_iterations():
    node_id = f"test-sweep-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool

    for _ in range(3):
        await builder.reconcile_stale_calls()
        await builder.reconcile_dead_nodes(stale_after_seconds=45)
        await builder.reconcile_inactive_calls(inactive_after_seconds=45)
        # Only this pool's backends: other suites on the same server may legitimately hold transactions.
        conns = await asyncio.gather(*(pool.acquire() for _ in range(pool.get_size())))
        try:
            pids = [await conn.fetchval("SELECT pg_backend_pid()") for conn in conns]
            idle_in_txn = await conns[0].fetch(
                "SELECT pid, query FROM pg_stat_activity WHERE state = 'idle in transaction' AND pid = ANY($1)",
                pids,
            )
        finally:
            await asyncio.gather(*(pool.release(conn) for conn in conns))
        assert idle_in_txn == []

    await builder.close()


# ── record_live_stage (Live Calls Monitoring, T18/T19) ───────────────────

async def test_record_live_stage_is_fire_and_forget_and_updates_the_column():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-live-stage-{uuid.uuid4().hex[:8]}"
    # begin_call (not the raw-insert helper) so the cached tenant slug used by record_live_stage() is populated.
    builder.begin_call(session_id, "default", "call-1")

    builder.record_live_stage(session_id, "waiting_for_human")
    # Returned synchronously (no DB round trip); the write runs as a background task.
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
    """Writes are serialized per session via _spawn(), so a delayed earlier write never clobbers a later one."""
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-live-stage-order-{uuid.uuid4().hex[:8]}"
    # begin_call (not the raw-insert helper) so the cached tenant slug used by record_live_stage() is populated.
    builder.begin_call(session_id, "default", "call-1")

    original_write = builder._record_live_stage

    async def _slow_for_waiting_for_human(sid, tenant_slug, stage):
        if stage == "waiting_for_human":
            await asyncio.sleep(0.2)
        await original_write(sid, tenant_slug, stage)

    monkeypatch.setattr(builder, "_record_live_stage", _slow_for_waiting_for_human)

    builder.record_live_stage(session_id, "waiting_for_human")  # on_transfer_initiated
    builder.record_live_stage(session_id, "human_connected")    # on_transfer_completed, right after

    await builder._chains[session_id]
    # Margin past the injected 0.2s delay so the final state is checked after both writes land.
    await asyncio.sleep(0.3)

    row = await pool.fetchrow("SELECT live_stage FROM calls WHERE session_id = $1", session_id)
    assert row["live_stage"] == "human_connected"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


# ── correct_turn_reply: the heard text replaces a turn's sent-based reply ──

async def test_correct_turn_reply_updates_the_row_after_its_insert_on_the_same_chain():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-correct-reply-{uuid.uuid4().hex[:8]}"
    builder.begin_call(session_id, "default", "call-1")

    turn_number = builder.record_turn(session_id, "hello", 0.9, "Sentence one.", True)
    builder.correct_turn_reply(session_id, turn_number, "Sentence")
    await builder._chains[session_id]

    assert turn_number == 1
    assert await pool.fetchval(
        "SELECT ai_response FROM transcript_entries WHERE session_id = $1 AND turn_number = $2",
        session_id, turn_number,
    ) == "Sentence"

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_correct_turn_reply_update_is_scoped_to_the_calls_tenant(monkeypatch):
    # RLS is inert under the superuser DSN, so assert the statement carries its own tenant predicate.
    conn = AsyncMock()

    @asynccontextmanager
    async def fake_tenant_conn(pool, *, explicit_tenant, reason):
        assert explicit_tenant == "acme"
        yield conn

    monkeypatch.setattr(transcript_builder, "tenant_conn", fake_tenant_conn)
    builder = TranscriptBuilder(pool=object())
    builder._tenant_slugs["s1"] = "acme"

    builder.correct_turn_reply("s1", 3, "Sentence\x00", newly_interrupted=True)
    await builder._chains["s1"]

    sql, *args = conn.execute.call_args.args
    assert "FROM calls WHERE session_id = $1 AND tenant_id = $4" in sql
    assert "interrupted = interrupted OR $5" in sql
    assert args == ["s1", 3, "Sentence", "acme", True]   # NUL stripped
    assert builder._barge_in_counts["s1"] == 1


async def test_correct_turn_reply_writes_nothing_after_end_call():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-correct-reply-ended-{uuid.uuid4().hex[:8]}"
    builder.begin_call(session_id, "default", "call-1")
    turn_number = builder.record_turn(session_id, "hello", 0.9, "Sentence one.", True)
    builder.end_call(session_id, "stream_ended", score_sentiment=False)
    chain = builder._chains[session_id]

    builder.correct_turn_reply(session_id, turn_number, "too late")

    assert builder._chains[session_id] is chain   # nothing was queued behind the end
    await chain
    assert await pool.fetchval(
        "SELECT ai_response FROM transcript_entries WHERE session_id = $1", session_id,
    ) == "Sentence one."

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_record_turn_returns_none_and_correction_is_a_noop_without_a_pool():
    builder = TranscriptBuilder(pool=None)

    assert builder.record_turn("s", "hello", 0.9, "hi", False) is None
    builder.correct_turn_reply("s", 1, "hi")
    assert builder._chains == {}


# ── sentiment scoring at end_call ─────────────────────────────────────────
# Scoring never blocks finalization: the call must end correctly even if the scorer fails.

class _StubScorer:
    """Stands in for SentimentScorer. Records what it was handed so the
    transcript actually read back out of Postgres can be asserted on."""

    def __init__(self, result=None, *, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.seen_turns = None
        self.calls = 0

    async def score(self, turns):
        self.calls += 1
        self.seen_turns = turns
        if self._raises is not None:
            raise self._raises
        return self._result


class _Result:
    def __init__(self, label: str, reason: str) -> None:
        self.label = label
        self.reason = reason


async def _finish_call(builder, session_id: str) -> None:
    builder.begin_call(session_id, "default", "call-1")
    builder.record_turn(session_id, "this is the third time I've called", 0.9, "I'm sorry about that", False)
    builder.end_call(session_id, "stream_ended", score_sentiment=True)
    await builder._chains[session_id]


async def _cleanup(pool, session_id: str) -> None:
    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def test_sentiment_switched_off_skips_scoring_but_still_ends_the_call():
    scorer = _StubScorer(_Result("frustrated", "should never be written"))
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], sentiment=scorer)
    pool = builder._pool
    session_id = f"test-sentiment-off-{uuid.uuid4().hex[:8]}"

    builder.begin_call(session_id, "default", "call-1")
    builder.record_turn(session_id, "hello", 0.9, "hi there", False)
    builder.end_call(session_id, "stream_ended", score_sentiment=False)
    await builder._chains[session_id]

    row = await pool.fetchrow(
        "SELECT ended_at, close_reason, sentiment FROM calls WHERE session_id = $1", session_id,
    )
    assert row["ended_at"] is not None
    assert row["close_reason"] == "stream_ended"
    assert row["sentiment"] is None
    assert scorer.calls == 0

    await _cleanup(pool, session_id)
    await builder.close()


async def test_end_call_writes_sentiment_from_the_persisted_transcript():
    scorer = _StubScorer(_Result("frustrated", "caller had to call three times"))
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], sentiment=scorer)
    pool = builder._pool
    session_id = f"test-sentiment-{uuid.uuid4().hex[:8]}"

    await _finish_call(builder, session_id)

    row = await pool.fetchrow(
        "SELECT ended_at, sentiment, sentiment_reason FROM calls WHERE session_id = $1", session_id,
    )
    assert row["ended_at"] is not None
    assert row["sentiment"] == "frustrated"
    assert row["sentiment_reason"] == "caller had to call three times"

    # Scored from transcript_entries, so end_call() needn't keep transcripts in memory.
    assert scorer.seen_turns is not None
    assert scorer.seen_turns[0].caller_text == "this is the third time I've called"

    await _cleanup(pool, session_id)
    await builder.close()


async def test_a_declined_score_leaves_sentiment_null_and_still_finalizes():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], sentiment=_StubScorer(None))
    pool = builder._pool
    session_id = f"test-sentiment-none-{uuid.uuid4().hex[:8]}"

    await _finish_call(builder, session_id)

    row = await pool.fetchrow(
        "SELECT ended_at, close_reason, turn_count, sentiment FROM calls WHERE session_id = $1", session_id,
    )
    assert row["sentiment"] is None, "NULL means 'never scored' — never coerce to 'neutral'"
    assert row["ended_at"] is not None
    assert row["close_reason"] == "stream_ended"
    assert row["turn_count"] == 1

    await _cleanup(pool, session_id)
    await builder.close()


async def test_a_raising_scorer_never_breaks_call_finalization():
    scorer = _StubScorer(raises=RuntimeError("model unreachable"))
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], sentiment=scorer)
    pool = builder._pool
    session_id = f"test-sentiment-boom-{uuid.uuid4().hex[:8]}"

    await _finish_call(builder, session_id)

    row = await pool.fetchrow(
        "SELECT ended_at, duration_ms, sentiment FROM calls WHERE session_id = $1", session_id,
    )
    assert scorer.calls == 1
    assert row["ended_at"] is not None
    assert row["duration_ms"] is not None
    assert row["sentiment"] is None

    await _cleanup(pool, session_id)
    await builder.close()


async def test_no_scorer_configured_skips_scoring_entirely():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-sentiment-off-{uuid.uuid4().hex[:8]}"

    await _finish_call(builder, session_id)

    row = await pool.fetchrow("SELECT ended_at, sentiment FROM calls WHERE session_id = $1", session_id)
    assert row["ended_at"] is not None
    assert row["sentiment"] is None

    await _cleanup(pool, session_id)
    await builder.close()


async def test_close_drains_an_in_flight_sentiment_write():
    """close() waits for an in-flight sentiment write instead of dropping it."""
    slow = asyncio.Event()

    class _SlowScorer:
        async def score(self, turns):
            await slow.wait()
            return _Result("positive", "caller thanked the agent")

    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], sentiment=_SlowScorer())
    pool = builder._pool
    session_id = f"test-sentiment-drain-{uuid.uuid4().hex[:8]}"

    builder.begin_call(session_id, "default", "call-1")
    builder.record_turn(session_id, "thank you so much", 0.9, "happy to help", False)
    builder.end_call(session_id, "stream_ended", score_sentiment=True)

    # Let the chain reach the scorer, then shut down while it is still there.
    await asyncio.sleep(0.05)
    close_task = asyncio.create_task(builder.close())
    await asyncio.sleep(0.05)
    assert not close_task.done(), "close() should be waiting on the in-flight chain"
    slow.set()
    await close_task

    # A fresh pool — the one under test is closed.
    verify = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    row = await verify._pool.fetchrow(
        "SELECT sentiment FROM calls WHERE session_id = $1", session_id,
    )
    assert row["sentiment"] == "positive"

    await _cleanup(verify._pool, session_id)
    await verify.close()


# ── record_detected_languages (multilingual agents) ──────────────────────

async def test_record_detected_languages_updates_the_column():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-langs-{uuid.uuid4().hex[:8]}"
    builder.begin_call(session_id, "default", "call-1")

    builder.record_detected_languages(session_id, ["en", "hi"])
    await builder._chains[session_id]

    row = await pool.fetchrow("SELECT detected_languages FROM calls WHERE session_id = $1", session_id)
    assert row["detected_languages"] == ["en", "hi"]

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_record_detected_languages_is_noop_without_pool_or_languages():
    builder = TranscriptBuilder(pool=None)
    builder.record_detected_languages("s", ["hi"])
    assert builder._chains == {}


# ── record_turn: NUL stripping, tool_calls, barge-in accounting ───────────

async def test_record_turn_strips_nul_and_keeps_turn_numbers_contiguous():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-nul-{uuid.uuid4().hex[:8]}"
    builder.begin_call(session_id, "default", "call-1")

    builder.record_turn(session_id, "a\x00b", 0.9, "c\x00d", False)
    builder.record_turn(session_id, "next", 0.9, "reply", False)
    await builder._chains[session_id]

    rows = await pool.fetch(
        "SELECT turn_number, caller_text, ai_response FROM transcript_entries "
        "WHERE session_id = $1 ORDER BY turn_number", session_id,
    )
    assert [(r["turn_number"], r["caller_text"], r["ai_response"]) for r in rows] == [
        (1, "ab", "cd"), (2, "next", "reply"),
    ]

    await _cleanup(pool, session_id)
    await builder.close()


async def test_record_turn_stores_only_tool_name_and_status():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-tools-{uuid.uuid4().hex[:8]}"
    builder.begin_call(session_id, "default", "call-1")

    builder.record_turn(
        session_id, "pay", 0.9, "done", True,
        tool_calls=[{"name": "pay\x00_order", "status": "success", "arguments": "card=4111111111111111"}],
    )
    builder.record_turn(session_id, "again", 0.9, "ok", False, tool_calls=[])
    await builder._chains[session_id]

    rows = await pool.fetch(
        "SELECT tool_calls::text AS raw FROM transcript_entries WHERE session_id = $1 ORDER BY turn_number",
        session_id,
    )
    assert json.loads(rows[0]["raw"]) == [{"name": "pay_order", "status": "success"}]
    assert "4111" not in rows[0]["raw"]
    assert rows[1]["raw"] is None  # no tools -> NULL, not "[]"

    await _cleanup(pool, session_id)
    await builder.close()


async def test_count_barge_in_false_leaves_an_interrupted_turn_uncounted():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-barge-{uuid.uuid4().hex[:8]}"
    builder.begin_call(session_id, "default", "call-1")

    builder.record_turn(session_id, "hung up", 0.9, "partial", True, count_barge_in=False)
    assert builder._barge_in_counts[session_id] == 0
    builder.record_turn(session_id, "real barge", 0.9, "partial", True)
    assert builder._barge_in_counts[session_id] == 1
    builder.record_turn(session_id, "forced", 0.9, "full", False, count_barge_in=True)
    assert builder._barge_in_counts[session_id] == 2

    builder.end_call(session_id, "stream_ended", score_sentiment=False)
    await builder._chains[session_id]
    row = await pool.fetchrow("SELECT barge_in_count FROM calls WHERE session_id = $1", session_id)
    assert row["barge_in_count"] == 2

    await _cleanup(pool, session_id)
    await builder.close()


# ── record_summary ─────────────────────────────────────────────────────────

async def test_record_summary_updates_the_calls_row_scoped_to_the_tenant(monkeypatch):
    executed: list[tuple] = []

    class _Conn:
        async def execute(self, sql, *args):
            executed.append((sql, args))

    @asynccontextmanager
    async def _fake_tenant_conn(pool, **kwargs):
        executed.append(("tenant_conn", (kwargs["explicit_tenant"], kwargs["reason"])))
        yield _Conn()

    monkeypatch.setattr(transcript_builder, "tenant_conn", _fake_tenant_conn)
    builder = TranscriptBuilder(pool=object())
    builder._tenant_slugs["s1"] = "acme"

    builder.record_summary("s1", "caller\x00 wants a refund")
    await builder._chains["s1"]

    assert executed[0] == ("tenant_conn", ("acme", "conversation-session-write"))
    sql, args = executed[1]
    assert sql == "UPDATE calls SET session_summary = $2 WHERE session_id = $1 AND tenant_id = $3"
    assert args == ("s1", "caller wants a refund", "acme")


async def test_record_summary_is_noop_without_pool():
    builder = TranscriptBuilder(pool=None)
    builder.record_summary("s", "summary")
    assert builder._chains == {}


# ── answering agent after a delegate handoff ───────────────────────────────

async def _insert_agent(pool) -> uuid.UUID:
    return await pool.fetchval(
        "INSERT INTO agents (tenant_id, slug, name) "
        "SELECT id, $1, 'Test Agent' FROM tenants WHERE slug = 'default' RETURNING id",
        f"test-answering-{uuid.uuid4().hex[:8]}",
    )


async def test_second_begin_call_with_a_different_agent_sets_answering_agent():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    flow_agent, sales_agent = await _insert_agent(pool), await _insert_agent(pool)
    session_id = f"test-answering-{uuid.uuid4().hex[:8]}"

    builder.begin_call(session_id, "default", "call-1", agent_id=flow_agent)
    await builder._chains[session_id]
    row = await pool.fetchrow(
        "SELECT agent_id, answering_agent_id FROM calls WHERE session_id = $1", session_id,
    )
    assert (row["agent_id"], row["answering_agent_id"]) == (flow_agent, None)

    builder.begin_call(session_id, "default", "call-1", agent_id=flow_agent)  # same agent: no handoff
    await builder._chains[session_id]
    row = await pool.fetchrow(
        "SELECT agent_id, answering_agent_id FROM calls WHERE session_id = $1", session_id,
    )
    assert (row["agent_id"], row["answering_agent_id"]) == (flow_agent, None)

    builder.begin_call(session_id, "default", "call-1", agent_id=sales_agent)  # delegate takes over
    await builder._chains[session_id]
    row = await pool.fetchrow(
        "SELECT agent_id, answering_agent_id FROM calls WHERE session_id = $1", session_id,
    )
    assert (row["agent_id"], row["answering_agent_id"]) == (flow_agent, sales_agent)

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM agents WHERE id = ANY($1::uuid[])", [flow_agent, sales_agent])
    await builder.close()


# ── reconcile sweeps derive turn_count from persisted rows ─────────────────

async def _insert_turns(pool, session_id: str, n: int) -> None:
    for i in range(1, n + 1):
        await pool.execute(
            "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
            "VALUES ($1, $2, 'q', 'a')", session_id, i,
        )


async def test_reconcile_stale_calls_derives_turn_count_from_transcript_rows():
    node_id = f"test-host:{uuid.uuid4().hex[:8]}"
    other_node = f"test-host:{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    with_turns, no_turns, others = (f"test-tc-{uuid.uuid4().hex[:8]}" for _ in range(3))
    await _insert_live_call(pool, with_turns, conv_node=node_id)
    await _insert_live_call(pool, no_turns, conv_node=node_id)
    await _insert_live_call(pool, others, conv_node=other_node)
    await _insert_turns(pool, with_turns, 3)
    await _insert_turns(pool, others, 2)

    await builder.reconcile_stale_calls()

    counts = {
        r["session_id"]: (r["turn_count"], r["ended_at"] is not None)
        for r in await pool.fetch(
            "SELECT session_id, turn_count, ended_at FROM calls WHERE session_id = ANY($1::text[])",
            [with_turns, no_turns, others],
        )
    }
    assert counts == {with_turns: (3, True), no_turns: (0, True), others: (0, False)}

    for sid in (with_turns, no_turns, others):
        await _cleanup(pool, sid)
    await builder.close()


async def test_reconcile_dead_nodes_derives_turn_count_from_transcript_rows():
    dead_node = f"test-dead-{uuid.uuid4().hex[:8]}"
    live_node = f"test-live-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=live_node)
    pool = builder._pool
    dead_call, live_call = (f"test-tc-{uuid.uuid4().hex[:8]}" for _ in range(2))
    await _insert_live_call(pool, dead_call, conv_node=dead_node)
    await _insert_live_call(pool, live_call, conv_node=live_node)
    await _insert_turns(pool, dead_call, 3)
    await _insert_turns(pool, live_call, 2)
    await builder.heartbeat()

    await builder.reconcile_dead_nodes(stale_after_seconds=45)

    rows = {
        r["session_id"]: (r["turn_count"], r["ended_at"] is not None)
        for r in await pool.fetch(
            "SELECT session_id, turn_count, ended_at FROM calls WHERE session_id = ANY($1::text[])",
            [dead_call, live_call],
        )
    }
    assert rows == {dead_call: (3, True), live_call: (0, False)}

    for sid in (dead_call, live_call):
        await _cleanup(pool, sid)
    await pool.execute("DELETE FROM conversation_node_heartbeats WHERE node_id = $1", live_node)
    await builder.close()


async def test_reconcile_inactive_calls_derives_turn_count_from_transcript_rows():
    node_id = f"test-node-{uuid.uuid4().hex[:8]}"
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"], node_id=node_id)
    pool = builder._pool
    silent, active = (f"test-tc-{uuid.uuid4().hex[:8]}" for _ in range(2))
    for sid in (silent, active):
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction, conv_node, started_at) "
            "VALUES ($1, 'default', 'inbound', $2, NOW() - INTERVAL '10 minutes')", sid, node_id,
        )
    await pool.execute(  # one old row: the call has turns but none inside the inactivity window
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response, created_at) "
        "VALUES ($1, 1, 'q', 'a', NOW() - INTERVAL '5 minutes'), ($1, 2, 'q', 'a', NOW() - INTERVAL '4 minutes')",
        silent,
    )
    await _insert_turns(pool, active, 1)  # created_at defaults to now: inside the window

    await builder.reconcile_inactive_calls(inactive_after_seconds=45)

    rows = {
        r["session_id"]: (r["turn_count"], r["ended_at"] is not None)
        for r in await pool.fetch(
            "SELECT session_id, turn_count, ended_at FROM calls WHERE session_id = ANY($1::text[])",
            [silent, active],
        )
    }
    assert rows == {silent: (2, True), active: (0, False)}

    for sid in (silent, active):
        await _cleanup(pool, sid)
    await builder.close()


async def test_every_reconcile_sweep_counts_transcript_rows_and_keeps_its_node_predicate(monkeypatch):
    executed: list[str] = []

    class _Conn:
        async def execute(self, sql, *args):
            executed.append(" ".join(sql.split()))
            return "UPDATE 0"

    @asynccontextmanager
    async def _fake_platform_conn(pool, **kwargs):
        yield _Conn()

    monkeypatch.setattr(transcript_builder, "platform_conn", _fake_platform_conn)
    builder = TranscriptBuilder(pool=object(), node_id="n1")

    await builder.reconcile_stale_calls()
    await builder.reconcile_dead_nodes(stale_after_seconds=45)
    await builder.reconcile_inactive_calls(inactive_after_seconds=45)

    node_predicates = ["conv_node = $1", "conv_node NOT IN (", "NOT EXISTS ("]
    assert len(executed) == 3
    for sql, predicate in zip(executed, node_predicates):
        assert ("turn_count = (SELECT COUNT(*) FROM transcript_entries te "
                "WHERE te.session_id = calls.session_id)") in sql
        assert predicate in sql
