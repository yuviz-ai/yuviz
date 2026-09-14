"""
T19 — an integration test driving each of the four transfer hooks
(on_transfer_initiated/on_transfer_completed/on_transfer_failed/
on_transfer_cancelled) through a real ConversationSession + a
PipelineConversationHandler wired to a REAL TranscriptBuilder, and observing
calls.live_stage land in Postgres — not just that record_live_stage() was
called, which test_transcript_builder.py's own tests already cover in
isolation from the FSM.

Reuses test_pipeline.py's _make_handler/_make_stt/_make_llm/_make_tts
helpers (same convention as test_pipeline_knowledge.py/
test_workflow_pipeline.py), passed a real `transcripts=` TranscriptBuilder
instead of the default None.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")

from ..event_bus import EventBus  # noqa: E402
from ..session import ConversationSession, SessionContext  # noqa: E402
from ..transcript_builder import TranscriptBuilder  # noqa: E402
from .test_pipeline import _make_handler, _make_llm, _make_stt, _make_tts  # noqa: E402


async def _insert_live_call(pool, session_id: str) -> None:
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, 'default', 'inbound')",
        session_id,
    )


async def _make_session_with_real_transcripts(builder: TranscriptBuilder, session_id: str):
    handler = _make_handler(_make_stt(), _make_llm(["ok."]), _make_tts(), transcripts=builder)
    ctx = SessionContext(session_id=session_id)
    bus = EventBus()
    session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
    session.session_ready()
    return session


async def test_on_transfer_initiated_sets_waiting_for_human():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-stage-init-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, session_id)
    session = await _make_session_with_real_transcripts(builder, session_id)

    session.on_transfer_initiated("cold", "+15551234567", "escalation_threshold_exceeded")
    await builder._chains[session_id]

    row = await pool.fetchrow("SELECT live_stage FROM calls WHERE session_id = $1", session_id)
    assert row["live_stage"] == "waiting_for_human"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_on_transfer_completed_sets_human_connected():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-stage-completed-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, session_id)
    session = await _make_session_with_real_transcripts(builder, session_id)

    session.on_transfer_initiated("cold", "+15551234567", "x")
    await session.on_transfer_completed("+15551234567")
    await builder._chains[session_id]

    row = await pool.fetchrow("SELECT live_stage FROM calls WHERE session_id = $1", session_id)
    assert row["live_stage"] == "human_connected"

    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_on_transfer_failed_reverts_to_ai():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-stage-failed-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, session_id)
    session = await _make_session_with_real_transcripts(builder, session_id)

    session.on_transfer_initiated("cold", "+15551234567", "x")
    async for _ in session.on_transfer_failed("+15551234567", "hangup_before_bridge"):
        pass
    await builder._chains[session_id]

    row = await pool.fetchrow("SELECT live_stage FROM calls WHERE session_id = $1", session_id)
    assert row["live_stage"] == "ai"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()


async def test_on_transfer_cancelled_reverts_to_ai():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    pool = builder._pool
    session_id = f"test-stage-cancelled-{uuid.uuid4().hex[:8]}"
    await _insert_live_call(pool, session_id)
    session = await _make_session_with_real_transcripts(builder, session_id)

    # A real prior attempt, then the NEXT one gets barged-in before
    # dispatch — on_transfer_cancelled must revert 'waiting_for_human' back
    # to 'ai', not leave the stale value from the dropped attempt.
    session.on_transfer_initiated("cold", "+15551234567", "x")
    session.on_transfer_cancelled("tx-2")
    await builder._chains[session_id]

    row = await pool.fetchrow("SELECT live_stage FROM calls WHERE session_id = $1", session_id)
    assert row["live_stage"] == "ai"

    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
    await builder.close()
