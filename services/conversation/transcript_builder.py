"""TranscriptBuilder — fire-and-forget persistence to calls/transcript_entries; never adds call latency.
Writes per session_id are chained in order (FK: calls row first). No-op when pool is None."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import asyncpg

from libs.tenancy import platform_conn, tenant_conn

from .bounded import POOL_COMMAND_TIMEOUT_S, POOL_RELEASE_TIMEOUT_S
from .sentiment import SentimentScorer, Turn

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnLatency:
    """Per-turn voice-to-voice timing; unset fields are stored as NULL, never zero."""
    stt_ms:            float | None = None
    llm_ms:            float | None = None
    tts_ms:            float | None = None
    voice_to_voice_ms: float | None = None
    stt_engine:        str | None = None
    llm_engine:        str | None = None
    tts_engine:        str | None = None


class TranscriptBuilder:
    def __init__(
        self,
        pool: asyncpg.Pool | None,
        node_id: str | None = None,
        sentiment: "SentimentScorer | None" = None,
    ) -> None:
        self._pool = pool
        self._sentiment = sentiment
        # Scopes reconcile_stale_calls() so a restart never closes another instance's live calls.
        self._node_id = node_id
        self._chains:          dict[str, asyncio.Task] = {}
        self._turn_counts:     dict[str, int]           = {}
        self._barge_in_counts: dict[str, int]           = {}
        # Cached at begin_call() so every later write scopes its connection to the same tenant.
        self._tenant_slugs:    dict[str, str]            = {}

    @classmethod
    async def connect(
        cls,
        database_url: str | None,
        node_id: str | None = None,
        sentiment: "SentimentScorer | None" = None,
    ) -> "TranscriptBuilder":
        if not database_url:
            log.info("TranscriptBuilder disabled — POSTGRES_DSN not set")
            return cls(pool=None)
        pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=5, command_timeout=POOL_COMMAND_TIMEOUT_S,
        )
        log.info(
            "TranscriptBuilder connected node_id=%s sentiment=%s",
            node_id, "on" if sentiment is not None else "off",
        )
        return cls(pool=pool, node_id=node_id, sentiment=sentiment)

    async def close(self, *, drain_timeout_s: float = 25.0) -> None:
        """Drain outstanding write chains (bounded; above the sentiment scorer's ceiling), then close the pool."""
        if self._pool is None:
            return
        pending = [t for t in self._chains.values() if not t.done()]
        if pending:
            log.info("TranscriptBuilder: draining %d write chain(s)", len(pending))
            done, still_running = await asyncio.wait(pending, timeout=drain_timeout_s)
            for task in still_running:
                task.cancel()
            if still_running:
                log.warning(
                    "TranscriptBuilder: %d write chain(s) did not drain in %.0fs — cancelled",
                    len(still_running), drain_timeout_s,
                )
        await self._pool.close()

    async def reconcile_stale_calls(self) -> int:
        """At startup, close live calls left by a previous process with this node_id (duration left NULL).
        No-op without a node_id — never run unscoped."""
        if self._pool is None or not self._node_id:
            return 0
        async with platform_conn(self._pool, reason="conversation-reconcile-sweep") as conn:
            result = await conn.execute(
                "UPDATE calls SET ended_at = NOW(), close_reason = 'reconciled_stale', "
                "turn_count = (SELECT COUNT(*) FROM transcript_entries te WHERE te.session_id = calls.session_id) "
                "WHERE ended_at IS NULL AND conv_node = $1",
                self._node_id,
            )
        count = int(result.split()[-1]) if result else 0
        if count:
            log.warning(
                "TranscriptBuilder: reconciled %d stale live call(s) previously owned by node_id=%s",
                count, self._node_id,
            )
        return count

    async def heartbeat(self) -> None:
        """Liveness UPSERT for other instances' reconcile_dead_nodes(); shares no state with the call path.

        Raises on a DB failure: the heartbeat loop must tell a failed write from a good one."""
        if self._pool is None or not self._node_id:
            return
        async with platform_conn(
            self._pool, reason="conversation-reconcile-sweep", acquire_timeout=POOL_RELEASE_TIMEOUT_S,
        ) as conn:
            await conn.execute(
                "INSERT INTO conversation_node_heartbeats (node_id, last_seen_at) VALUES ($1, NOW()) "
                "ON CONFLICT (node_id) DO UPDATE SET last_seen_at = NOW()",
                self._node_id,
            )

    async def reconcile_dead_nodes(self, *, stale_after_seconds: int) -> int:
        """Close live calls owned by any node whose heartbeat is missing or older than stale_after_seconds."""
        if self._pool is None:
            return 0
        async with platform_conn(
            self._pool, reason="conversation-reconcile-sweep", acquire_timeout=POOL_RELEASE_TIMEOUT_S,
        ) as conn:
            result = await conn.execute(
                """
                UPDATE calls SET ended_at = NOW(), close_reason = 'reconciled_dead_node',
                    turn_count = (SELECT COUNT(*) FROM transcript_entries te WHERE te.session_id = calls.session_id)
                WHERE ended_at IS NULL
                  AND conv_node IS NOT NULL
                  AND conv_node NOT IN (
                      SELECT node_id FROM conversation_node_heartbeats
                      WHERE last_seen_at >= NOW() - ($1 * INTERVAL '1 second')
                  )
                """,
                stale_after_seconds,
            )
        count = int(result.split()[-1]) if result else 0
        if count:
            log.warning("TranscriptBuilder: reconciled %d call(s) owned by dead/silent node(s)", count)
        return count

    async def reconcile_inactive_calls(self, *, inactive_after_seconds: int) -> int:
        """Close zombie calls (client vanished, process alive) with no transcript activity in the window.
        Time-based and idempotent, so any instance may run it for any node."""
        if self._pool is None:
            return 0
        async with platform_conn(
            self._pool, reason="conversation-reconcile-sweep", acquire_timeout=POOL_RELEASE_TIMEOUT_S,
        ) as conn:
            result = await conn.execute(
                """
                UPDATE calls SET ended_at = NOW(), close_reason = 'reconciled_inactive',
                    turn_count = (SELECT COUNT(*) FROM transcript_entries te WHERE te.session_id = calls.session_id)
                WHERE ended_at IS NULL
                  AND started_at < NOW() - ($1 * INTERVAL '1 second')
                  AND NOT EXISTS (
                      SELECT 1 FROM transcript_entries te
                      WHERE te.session_id = calls.session_id
                        AND te.created_at >= NOW() - ($1 * INTERVAL '1 second')
                  )
                """,
                inactive_after_seconds,
            )
        count = int(result.split()[-1]) if result else 0
        if count:
            log.warning("TranscriptBuilder: reconciled %d inactive live call(s) (no activity for %ds)",
                        count, inactive_after_seconds)
        return count

    def begin_call(
        self,
        session_id:    str,
        tenant_id:     str,
        call_id:       str,
        direction:     str = "inbound",
        caller_number: str = "",
        called_number: str = "",
        agent_id:      str | None = None,
        agent_config_version: int | None = None,
    ) -> None:
        if self._pool is None:
            return
        tenant_slug = tenant_id or "default"
        # A repeat begin_call() for the same live call (delegate handoff) keeps its counters.
        self._turn_counts.setdefault(session_id, 0)
        self._barge_in_counts.setdefault(session_id, 0)
        self._tenant_slugs[session_id] = tenant_slug
        self._spawn(session_id, self._begin_call(
            session_id, tenant_slug, call_id, direction, caller_number, called_number,
            agent_id, agent_config_version,
        ))

    def record_turn(
        self,
        session_id:        str,
        caller_text:       str,
        caller_confidence: float,
        ai_response:       str,
        interrupted:       bool,
        latency:           "TurnLatency | None" = None,
        *,
        tool_calls:        list[dict[str, str]] | None = None,
        count_barge_in:    bool | None = None,
    ) -> int | None:
        """count_barge_in=None counts a barge-in exactly when the turn was interrupted; a turn cut
        short by hang-up passes False. tool_calls carries only name and status, never arguments.
        Returns the turn number, or None when there is no pool to write to."""
        if self._pool is None:
            return None
        # Before the turn number is taken: a NUL would fail the insert and leave a gap.
        caller_text = _strip_nul(caller_text)
        ai_response = _strip_nul(ai_response)
        tool_calls_json = (
            json.dumps([{"name": _strip_nul(t["name"]), "status": t["status"]} for t in tool_calls])
            if tool_calls else None
        )
        turn_number = self._turn_counts.get(session_id, 0) + 1
        self._turn_counts[session_id] = turn_number
        if interrupted if count_barge_in is None else count_barge_in:
            self._barge_in_counts[session_id] = self._barge_in_counts.get(session_id, 0) + 1
        self._spawn(session_id, self._record_turn(
            session_id, self._tenant_slugs.get(session_id), turn_number, caller_text,
            caller_confidence, ai_response, interrupted, latency or TurnLatency(), tool_calls_json,
        ))
        return turn_number

    def correct_turn_reply(
        self, session_id: str, turn_number: int, text: str, *,
        newly_interrupted: bool = False, count_barge_in: bool = True,
    ) -> None:
        """Replace a recorded turn's ai_response, e.g. with the part the caller actually heard.
        newly_interrupted: the turn was recorded as complete but the caller cut it off; it is marked
        interrupted, and counted as the barge-in it is unless count_barge_in is False (the same cut already
        counted for another turn). Queued behind the turn's INSERT. A no-op once the call
        has ended."""
        tenant_slug = self._tenant_slugs.get(session_id)
        if self._pool is None or tenant_slug is None:
            return
        if newly_interrupted and count_barge_in:
            self._barge_in_counts[session_id] = self._barge_in_counts.get(session_id, 0) + 1
        self._spawn(session_id, self._correct_turn_reply(
            session_id, tenant_slug, turn_number, _strip_nul(text), newly_interrupted,
        ))

    def record_summary(self, session_id: str, summary: str) -> None:
        """calls.session_summary: the post-transfer handoff summary. Never a transcript turn."""
        if self._pool is None:
            return
        self._spawn(session_id, self._record_summary(
            session_id, self._tenant_slugs.get(session_id), _strip_nul(summary),
        ))

    def record_workflow_outcome(
        self,
        session_id: str,
        *,
        nodes_visited: list[str] | None = None,
        disposition: str | None = None,
        extracted_variables: dict | None = None,
    ) -> None:
        """Path, disposition, and extracted vars for a workflow call. Spawn before end_call()."""
        if self._pool is None:
            return
        self._spawn(session_id, self._record_workflow_outcome(
            session_id, self._tenant_slugs.get(session_id), nodes_visited, disposition, extracted_variables,
        ))

    def record_detected_languages(self, session_id: str, languages: list[str]) -> None:
        """calls.detected_languages: languages the caller spoke, in order of first
        appearance (multilingual agents). Spawn before end_call()."""
        if self._pool is None or not languages:
            return
        self._spawn(session_id, self._record_detected_languages(
            session_id, self._tenant_slugs.get(session_id), list(languages),
        ))

    def record_live_stage(self, session_id: str, stage: str) -> None:
        """Set calls.live_stage ('ai' | 'waiting_for_human' | 'human_connected').
        The per-session chain guarantees a later stage is never overwritten by an earlier one."""
        if self._pool is None:
            return
        self._spawn(session_id, self._record_live_stage(session_id, self._tenant_slugs.get(session_id), stage))

    def discard_session(self, session_id: str) -> None:
        """Drop the in-memory bookkeeping for a session whose stream is gone. end_call() already
        does this on the normal path; this covers a teardown cut short before it."""
        self._turn_counts.pop(session_id, None)
        self._barge_in_counts.pop(session_id, None)
        self._tenant_slugs.pop(session_id, None)

    def end_call(self, session_id: str, close_reason: str,
                 final_state: str | None = None, *, score_sentiment: bool) -> None:
        # No default: sentiment is a per-agent opt-in, so every caller must say which.
        if self._pool is None:
            return
        turn_count     = self._turn_counts.pop(session_id, 0)
        barge_in_count = self._barge_in_counts.pop(session_id, 0)
        tenant_slug    = self._tenant_slugs.pop(session_id, None)
        self._spawn(session_id, self._end_call(
            session_id, tenant_slug, close_reason, turn_count, barge_in_count, final_state,
            score_sentiment=score_sentiment,
        ))
        chain = self._chains[session_id]
        # Only this call's chain: a later call reusing the id may already have replaced it.
        chain.add_done_callback(
            lambda _: self._chains.pop(session_id, None) if self._chains.get(session_id) is chain else None
        )

    def _spawn(self, session_id: str, coro) -> None:
        """Schedule *coro*, chained after any write already in flight for this
        session so writes land in order without blocking the caller."""
        prior = self._chains.get(session_id)

        async def _run() -> None:
            if prior is not None:
                try:
                    await prior
                except Exception:
                    pass  # prior step already logged its own failure
            await coro

        self._chains[session_id] = asyncio.create_task(_run())

    async def _begin_call(
        self,
        session_id:    str,
        tenant_slug:   str,
        call_id:       str,
        direction:     str,
        caller_number: str,
        called_number: str,
        agent_id:      str | None,
        agent_config_version: int | None,
    ) -> None:
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                # A conflict on another tenant's row must not count as success: every later write
                # is also tenant-guarded, so that stream's writes are dropped, not attached.
                owned = await conn.fetchval(
                    "INSERT INTO calls "
                    "(session_id, tenant_id, call_id, direction, caller_number, called_number, "
                    "agent_id, agent_config_version, conv_node) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (session_id) DO UPDATE SET answering_agent_id = "
                    "CASE WHEN EXCLUDED.agent_id IS DISTINCT FROM calls.agent_id "
                    "THEN EXCLUDED.agent_id ELSE calls.answering_agent_id END "
                    "WHERE calls.tenant_id = EXCLUDED.tenant_id RETURNING 1",
                    session_id, tenant_slug, call_id or None,
                    direction or "inbound", caller_number or None, called_number or None,
                    agent_id, agent_config_version, self._node_id,
                )
                if owned is None:
                    log.error("TranscriptBuilder: session_id belongs to another tenant's call "
                              "session=%s tenant=%s", session_id, tenant_slug)
        except Exception:
            log.exception("TranscriptBuilder: begin_call failed session=%s", session_id)

    async def _record_workflow_outcome(
        self, session_id: str, tenant_slug: str | None, nodes_visited: list[str] | None,
        disposition: str | None, extracted_variables: dict | None,
    ) -> None:
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "UPDATE calls SET nodes_visited = $2::jsonb, disposition = $3, "
                    "extracted_variables = $4::jsonb WHERE session_id = $1 AND tenant_id = $5",
                    session_id,
                    json.dumps(nodes_visited or []),
                    disposition,
                    json.dumps(extracted_variables or {}),
                    tenant_slug,
                )
        except Exception:
            log.exception("TranscriptBuilder: record_workflow_outcome failed session=%s", session_id)

    async def _record_detected_languages(
        self, session_id: str, tenant_slug: str | None, languages: list[str],
    ) -> None:
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "UPDATE calls SET detected_languages = $2 WHERE session_id = $1 AND tenant_id = $3",
                    session_id, languages, tenant_slug,
                )
        except Exception:
            log.exception("TranscriptBuilder: record_detected_languages failed session=%s", session_id)

    async def _record_summary(self, session_id: str, tenant_slug: str | None, summary: str) -> None:
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "UPDATE calls SET session_summary = $2 WHERE session_id = $1 AND tenant_id = $3",
                    session_id, summary, tenant_slug,
                )
        except Exception:
            log.exception("TranscriptBuilder: record_summary failed session=%s", session_id)

    async def _record_live_stage(self, session_id: str, tenant_slug: str | None, stage: str) -> None:
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "UPDATE calls SET live_stage = $2 WHERE session_id = $1 AND tenant_id = $3",
                    session_id, stage, tenant_slug,
                )
        except Exception:
            log.exception("TranscriptBuilder: record_live_stage failed session=%s stage=%s", session_id, stage)

    async def _record_turn(
        self,
        session_id:        str,
        tenant_slug:        str | None,
        turn_number:        int,
        caller_text:        str,
        caller_confidence:  float,
        ai_response:        str,
        interrupted:        bool,
        latency:            TurnLatency,
        tool_calls_json:    str | None,
    ) -> None:
        # JSONB mirror of the *_latency_ms columns so new fields don't need a migration.
        latency_json = json.dumps({
            "stt_ms": latency.stt_ms, "llm_ms": latency.llm_ms,
            "tts_ms": latency.tts_ms, "voice_to_voice_ms": latency.voice_to_voice_ms,
        })
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "INSERT INTO transcript_entries "
                    "(session_id, turn_number, caller_text, caller_confidence, ai_response, interrupted, "
                    "stt_engine, stt_latency_ms, llm_engine, llm_latency_ms, tts_engine, tts_latency_ms, "
                    "latency_ms, tool_calls) "
                    "SELECT $1::text, $2::int, $3::text, $4::float8, $5::text, $6::bool, $7::text, $8::int, "
                    "$9::text, $10::int, $11::text, $12::int, $13::jsonb, $15::jsonb "
                    "WHERE EXISTS (SELECT 1 FROM calls WHERE session_id = $1 AND tenant_id = $14)",
                    session_id, turn_number, caller_text, caller_confidence, ai_response, interrupted,
                    latency.stt_engine, _round_or_none(latency.stt_ms),
                    latency.llm_engine, _round_or_none(latency.llm_ms),
                    latency.tts_engine, _round_or_none(latency.tts_ms),
                    latency_json, tenant_slug, tool_calls_json,
                )
        except Exception:
            log.exception(
                "TranscriptBuilder: record_turn failed session=%s turn=%d", session_id, turn_number,
            )

    async def _correct_turn_reply(
        self, session_id: str, tenant_slug: str, turn_number: int, text: str, newly_interrupted: bool,
    ) -> None:
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "UPDATE transcript_entries SET ai_response = $3, interrupted = interrupted OR $5 "
                    "WHERE session_id = $1 AND turn_number = $2 "
                    "AND EXISTS (SELECT 1 FROM calls WHERE session_id = $1 AND tenant_id = $4)",
                    session_id, turn_number, text, tenant_slug, newly_interrupted,
                )
        except Exception:
            log.exception(
                "TranscriptBuilder: correct_turn_reply failed session=%s turn=%d", session_id, turn_number,
            )

    async def _end_call(
        self, session_id: str, tenant_slug: str | None, close_reason: str, turn_count: int,
        barge_in_count: int, final_state: str | None = None, *, score_sentiment: bool,
    ) -> None:
        turns: list[Turn] = []
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "UPDATE calls SET "
                    "ended_at = NOW(), "
                    "duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000, "
                    "close_reason = $2, turn_count = $3, barge_in_count = $4, "
                    "final_state = COALESCE($5, final_state) "
                    "WHERE session_id = $1 AND tenant_id = $6",
                    session_id, close_reason, turn_count, barge_in_count, final_state, tenant_slug,
                )
                # Read back rather than holding transcripts in memory; chained after all turn writes.
                if self._sentiment is not None and score_sentiment:
                    rows = await conn.fetch(
                        "SELECT te.caller_text, te.ai_response FROM transcript_entries te "
                        "JOIN calls c ON c.session_id = te.session_id "
                        "WHERE te.session_id = $1 AND c.tenant_id = $2 ORDER BY te.turn_number",
                        session_id, tenant_slug,
                    )
                    turns = [Turn(caller_text=r["caller_text"], ai_response=r["ai_response"])
                             for r in rows]
        except Exception:
            log.exception("TranscriptBuilder: end_call failed session=%s", session_id)
            return

        # Outside the connection block: a seconds-long LLM call would starve the small pool.
        if self._sentiment is not None and turns:
            await self._score_sentiment(session_id, tenant_slug, turns)

    async def _score_sentiment(
        self, session_id: str, tenant_slug: str | None, turns: list[Turn],
    ) -> None:
        """Best-effort: failure leaves calls.sentiment NULL ("never scored")."""
        try:
            result = await self._sentiment.score(turns)
        except Exception:
            log.exception("TranscriptBuilder: sentiment scoring failed session=%s", session_id)
            return
        if result is None:
            return
        try:
            async with tenant_conn(
                self._pool, explicit_tenant=tenant_slug, reason="conversation-session-write",
            ) as conn:
                await conn.execute(
                    "UPDATE calls SET sentiment = $2, sentiment_reason = $3 "
                    "WHERE session_id = $1 AND tenant_id = $4",
                    session_id, result.label, result.reason or None, tenant_slug,
                )
        except Exception:
            log.exception("TranscriptBuilder: sentiment write failed session=%s", session_id)


def _strip_nul(text: str) -> str:
    """Postgres text and jsonb both reject NUL, so one in a transcript would drop the whole row."""
    return text.replace("\x00", "")


def _round_or_none(value: float | None) -> int | None:
    return round(value) if value is not None else None
