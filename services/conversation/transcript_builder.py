"""
TranscriptBuilder — fire-and-forget persistence to the calls /
transcript_entries tables (database/schema.sql).

Every public method schedules its write as a background asyncio task and
returns immediately; none are awaited by the conversation pipeline, so a
slow or unreachable database can never add latency to a live call. Writes
for a given session_id are chained in order (not run concurrently) so a
transcript_entries row can never reach Postgres before the calls row
it references — required by the schema's foreign key.

Disabled (all methods become no-ops) when constructed with pool=None, i.e.
when POSTGRES_DSN is unset — persistence is opt-in.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import asyncpg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnLatency:
    """Per-turn voice-to-voice timing — see pipeline.py's on_speech_ended()
    for exactly what each field measures. All fields optional: a
    cancelled/barge-in/errored turn can legitimately have some or all of
    these unset (e.g. no audio ever got synthesized), and that's recorded
    as NULL, not zero — a zero would misleadingly read as "instant."""
    stt_ms:            float | None = None
    llm_ms:            float | None = None
    tts_ms:            float | None = None
    voice_to_voice_ms: float | None = None
    stt_engine:        str | None = None
    llm_engine:        str | None = None
    tts_engine:        str | None = None


class TranscriptBuilder:
    def __init__(self, pool: asyncpg.Pool | None, node_id: str | None = None) -> None:
        self._pool = pool
        # Identifies THIS process instance (see connect()'s docstring) —
        # stamped onto every call this instance begins, and used to scope
        # reconcile_stale_calls() so restarting one instance can never
        # touch a call another still-running instance is legitimately
        # serving (see project history, 2026-07-29: an earlier version of
        # reconcile_stale_calls() closed out EVERY live call platform-wide,
        # which is only safe with exactly one Conversation Service process
        # — this project runs two, :50051 and :50052, behind Envoy).
        self._node_id = node_id
        self._chains:          dict[str, asyncio.Task] = {}
        self._turn_counts:     dict[str, int]           = {}
        self._barge_in_counts: dict[str, int]           = {}

    @classmethod
    async def connect(cls, database_url: str | None, node_id: str | None = None) -> "TranscriptBuilder":
        if not database_url:
            log.info("TranscriptBuilder disabled — POSTGRES_DSN not set")
            return cls(pool=None)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        log.info("TranscriptBuilder connected node_id=%s", node_id)
        return cls(pool=pool, node_id=node_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def reconcile_stale_calls(self) -> int:
        """Call once at process startup, before serving any traffic. Any
        calls row with THIS instance's conv_node still marked live
        (ended_at IS NULL) necessarily belongs to a session from a
        PREVIOUS process that ran as this same node_id — a restart drops
        every gRPC stream the old process was holding, so nothing in this
        process can still be "finishing" that call. Scoped to conv_node =
        this instance's own id — never touches another instance's rows,
        which may be genuinely live right now. Left unreconciled, stale
        rows show up as permanently live in the Admin UI forever (see
        project history, 2026-07-29). Closed with a distinct close_reason
        so this is never confused with a call that ended normally;
        duration_ms is deliberately left NULL rather than computed from
        "now" — we don't actually know when it really ended, and a
        fabricated duration would be worse than no duration.

        No-ops (returns 0) if this instance has no node_id — reconciling
        with no scope would mean reconciling everything, exactly the bug
        this replaced, so "can't scope it" must mean "don't run it,"
        not "run it unscoped."""
        if self._pool is None or not self._node_id:
            return 0
        result = await self._pool.execute(
            "UPDATE calls SET ended_at = NOW(), close_reason = 'reconciled_stale' "
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
        """Called on a timer (see __main__.py's heartbeat loop) — proves
        this instance is still alive to reconcile_dead_nodes(), running on
        every OTHER instance. Deliberately its own tiny UPSERT, sharing no
        lock or state with the per-call pipeline — this must never be able
        to add latency to a live call (see project history, 2026-07-29:
        this was explicitly checked before building it, not assumed)."""
        if self._pool is None or not self._node_id:
            return
        try:
            await self._pool.execute(
                "INSERT INTO conversation_node_heartbeats (node_id, last_seen_at) VALUES ($1, NOW()) "
                "ON CONFLICT (node_id) DO UPDATE SET last_seen_at = NOW()",
                self._node_id,
            )
        except Exception:
            log.exception("TranscriptBuilder: heartbeat write failed node_id=%s", self._node_id)

    async def reconcile_dead_nodes(self, *, stale_after_seconds: int) -> int:
        """The general-case backstop reconcile_stale_calls() can't be:
        that one only fires when a crashed instance's exact node_id
        restarts. This closes out live calls owned by ANY node whose
        heartbeat has gone silent for stale_after_seconds — whether or not
        it ever comes back — checked periodically by every still-running
        instance (see __main__.py's heartbeat loop), not just by the dead
        node itself. A node with no heartbeat row at all (crashed before
        its first heartbeat, or heartbeats never ran) counts as dead too.
        Same close_reason discipline as reconcile_stale_calls(): distinct
        marker, duration_ms left NULL, never fabricated."""
        if self._pool is None:
            return 0
        result = await self._pool.execute(
            """
            UPDATE calls SET ended_at = NOW(), close_reason = 'reconciled_dead_node'
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
        """The backstop reconcile_stale_calls()/reconcile_dead_nodes() can't
        be: both of those only fire when an entire Conversation Service
        process has crashed or restarted. Neither catches the far more
        common real-world case — a single call's client disconnects
        uncleanly (browser tab killed, laptop slept, network dropped) while
        its owning process keeps running perfectly normally. WebSocket/TCP
        close events aren't guaranteed to ever reach the server in that
        case, so nothing else notices; the row would otherwise stay "live"
        in the Admin UI forever (confirmed live 2026-08-02: a webcall test
        session's browser vanished mid-farewell, the process never
        restarted, and the row sat with ended_at IS NULL indefinitely even
        though lsof showed zero actual open connections).

        A call counts as inactive once it's both older than
        inactive_after_seconds AND has had no transcript_entries write in
        that same window — the started_at floor means a call that hasn't
        had its first turn yet is never mistaken for stale just because
        transcript_entries has nothing for it. Deliberately NOT scoped to
        this instance's own node_id (unlike reconcile_stale_calls): this is
        a pure time-based heuristic on DB timestamps, not a claim about
        which process's in-memory state is authoritative, so any
        still-running instance can safely reconcile any node's inactive
        call — same posture as reconcile_dead_nodes(), and harmless to run
        redundantly on multiple instances (idempotent UPDATE...WHERE
        ended_at IS NULL). inactive_after_seconds should be set well above
        any legitimate silence — the Gateway's own no-speech-timeout
        already ends a genuinely silent-caller call within ~19s (see
        project history), so this is a last-resort backstop for zombie
        connections, not a substitute for that. Same close_reason
        discipline as the other two reconcile methods: distinct marker,
        duration_ms never fabricated."""
        if self._pool is None:
            return 0
        result = await self._pool.execute(
            """
            UPDATE calls SET ended_at = NOW(), close_reason = 'reconciled_inactive'
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

    # ── Public API — all fire-and-forget ────────────────────────────────────

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
        self._turn_counts[session_id] = 0
        self._barge_in_counts[session_id] = 0
        self._spawn(session_id, self._begin_call(
            session_id, tenant_id, call_id, direction, caller_number, called_number,
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
    ) -> None:
        if self._pool is None:
            return
        turn_number = self._turn_counts.get(session_id, 0) + 1
        self._turn_counts[session_id] = turn_number
        if interrupted:
            self._barge_in_counts[session_id] = self._barge_in_counts.get(session_id, 0) + 1
        self._spawn(session_id, self._record_turn(
            session_id, turn_number, caller_text, caller_confidence, ai_response, interrupted,
            latency or TurnLatency(),
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
            session_id, nodes_visited, disposition, extracted_variables,
        ))

    def end_call(self, session_id: str, close_reason: str,
                 final_state: str | None = None) -> None:
        if self._pool is None:
            return
        turn_count     = self._turn_counts.pop(session_id, 0)
        barge_in_count = self._barge_in_counts.pop(session_id, 0)
        self._spawn(session_id, self._end_call(
            session_id, close_reason, turn_count, barge_in_count, final_state,
        ))
        # Drop the chain once this session's final write completes — nothing
        # will call _spawn() for this session_id again after end_call().
        self._chains[session_id].add_done_callback(lambda _: self._chains.pop(session_id, None))

    # ── Internal ─────────────────────────────────────────────────────────────

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
        tenant_id:     str,
        call_id:       str,
        direction:     str,
        caller_number: str,
        called_number: str,
        agent_id:      str | None,
        agent_config_version: int | None,
    ) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO calls "
                    "(session_id, tenant_id, call_id, direction, caller_number, called_number, "
                    "agent_id, agent_config_version, conv_node) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (session_id) DO NOTHING",
                    session_id, tenant_id or "default", call_id or None,
                    direction or "inbound", caller_number or None, called_number or None,
                    agent_id, agent_config_version, self._node_id,
                )
        except Exception:
            log.exception("TranscriptBuilder: begin_call failed session=%s", session_id)

    async def _record_workflow_outcome(
        self, session_id: str, nodes_visited: list[str] | None,
        disposition: str | None, extracted_variables: dict | None,
    ) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE calls SET nodes_visited = $2::jsonb, disposition = $3, "
                    "extracted_variables = $4::jsonb WHERE session_id = $1",
                    session_id,
                    json.dumps(nodes_visited or []),
                    disposition,
                    json.dumps(extracted_variables or {}),
                )
        except Exception:
            log.exception("TranscriptBuilder: record_workflow_outcome failed session=%s", session_id)

    async def _record_turn(
        self,
        session_id:        str,
        turn_number:        int,
        caller_text:        str,
        caller_confidence:  float,
        ai_response:        str,
        interrupted:        bool,
        latency:            TurnLatency,
    ) -> None:
        # latency_ms JSONB mirrors the individual *_latency_ms columns —
        # kept alongside them (not instead of) so a future latency field
        # doesn't need a new column, matching the schema's own original
        # intent (see its comment: {"vad_hold":..,"stt":..,"llm":..,"tts":..}).
        latency_json = json.dumps({
            "stt_ms": latency.stt_ms, "llm_ms": latency.llm_ms,
            "tts_ms": latency.tts_ms, "voice_to_voice_ms": latency.voice_to_voice_ms,
        })
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO transcript_entries "
                    "(session_id, turn_number, caller_text, caller_confidence, ai_response, interrupted, "
                    "stt_engine, stt_latency_ms, llm_engine, llm_latency_ms, tts_engine, tts_latency_ms, "
                    "latency_ms) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb)",
                    session_id, turn_number, caller_text, caller_confidence, ai_response, interrupted,
                    latency.stt_engine, _round_or_none(latency.stt_ms),
                    latency.llm_engine, _round_or_none(latency.llm_ms),
                    latency.tts_engine, _round_or_none(latency.tts_ms),
                    latency_json,
                )
        except Exception:
            log.exception(
                "TranscriptBuilder: record_turn failed session=%s turn=%d", session_id, turn_number,
            )

    async def _end_call(
        self, session_id: str, close_reason: str, turn_count: int, barge_in_count: int,
        final_state: str | None = None,
    ) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE calls SET "
                    "ended_at = NOW(), "
                    "duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000, "
                    "close_reason = $2, turn_count = $3, barge_in_count = $4, "
                    "final_state = COALESCE($5, final_state) "
                    "WHERE session_id = $1",
                    session_id, close_reason, turn_count, barge_in_count, final_state,
                )
        except Exception:
            log.exception("TranscriptBuilder: end_call failed session=%s", session_id)


def _round_or_none(value: float | None) -> int | None:
    return round(value) if value is not None else None
