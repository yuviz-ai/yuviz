"""
SessionFinalizer — post-transfer cleanup as a pipeline of IFinalizationSteps,
run before the gateway tears down its CallSession.

Idempotent per session_id: the gateway may retry the notification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .metrics import IMetrics, NullMetrics
from .providers.interfaces import ChatMessage, ILLM
from .transcript_builder import TranscriptBuilder

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    "Summarize this conversation in 1-2 sentences for an internal call log. "
    "Focus on what the caller wanted and how it was resolved (or why it "
    "wasn't). Do not address the caller — this is an internal note, not a "
    "reply to them. Write it in English, whatever language the call was in."
)


class FinalizationStatus(Enum):
    """Outcome of one finalize() run; TIMED_OUT means the summary used its fallback."""
    RUNNING   = "running"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    FAILED    = "failed"


@dataclass
class FinalizationContext:
    """Mutable state threaded through the step pipeline."""
    session_id:   str
    history:      list[ChatMessage]
    llm:          ILLM | None
    cancel_event: object  # asyncio.Event | None
    transcripts:  TranscriptBuilder | None
    metrics:      IMetrics
    started_at:   float = field(default_factory=time.monotonic)
    # From start_summary_early(); SummaryStep awaits it instead of generating afresh.
    precomputed_summary_task: "asyncio.Task[str] | None" = None

    summary:            str  = ""
    summary_generated:   bool = False
    transcript_written:  bool = False
    timed_out:           bool = False


@dataclass(frozen=True)
class FinalizationResult:
    """Public outcome of finalize()."""
    summary:            str
    summary_generated:  bool
    transcript_written:  bool
    status:             FinalizationStatus


class IFinalizationStep(Protocol):
    """One stateless cleanup action; per-session state lives on the context."""
    name: str

    async def run(self, ctx: FinalizationContext) -> None: ...


# ── Steps ─────────────────────────────────────────────────────────────────────

class StopRuntimeStep:
    """Stop in-flight LLM generation before SummaryStep runs."""
    name = "stop_agent_runtime"

    async def run(self, ctx: FinalizationContext) -> None:
        if ctx.cancel_event is not None:
            ctx.cancel_event.set()


class StopToolExecutorStep:
    """No-op placeholder."""
    name = "stop_tool_executor"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class CancelTimersStep:
    """No-op: no timers are owned here."""
    name = "cancel_timers"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class MemoryFlushStep:
    """No-op: there is no separate memory store."""
    name = "flush_memory_manager"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class SummaryStep:
    """LLM summary bounded by timeout_s (the gateway's teardown waits on it); falls back on timeout."""
    name = "generate_summary"

    _FALLBACK = "Conversation summary unavailable (generation timed out)."

    def __init__(self, timeout_s: float = 3.0) -> None:
        self._timeout_s = timeout_s

    async def run(self, ctx: FinalizationContext) -> None:
        if ctx.precomputed_summary_task is not None:
            # Usually already done; the same timeout still applies.
            try:
                ctx.summary = await asyncio.wait_for(
                    asyncio.shield(ctx.precomputed_summary_task), timeout=self._timeout_s,
                )
                ctx.summary_generated = bool(ctx.summary)
            except asyncio.TimeoutError:
                log.warning(
                    "SessionFinalizer: precomputed summary not ready after %.1fs session=%s",
                    self._timeout_s, ctx.session_id,
                )
                ctx.metrics.increment("conversation_finalize_timeout_total")
                ctx.summary = self._FALLBACK
                ctx.summary_generated = False
                ctx.timed_out = True
            except Exception:
                log.exception(
                    "SessionFinalizer: precomputed summary generation failed session=%s",
                    ctx.session_id,
                )
                ctx.summary = self._FALLBACK
                ctx.summary_generated = False
            return

        if not ctx.history or ctx.llm is None:
            return
        try:
            ctx.summary = await asyncio.wait_for(
                self._generate(ctx.history, ctx.llm), timeout=self._timeout_s,
            )
            ctx.summary_generated = bool(ctx.summary)
        except asyncio.TimeoutError:
            log.warning(
                "SessionFinalizer: summary generation timed out after %.1fs session=%s",
                self._timeout_s, ctx.session_id,
            )
            ctx.metrics.increment("conversation_finalize_timeout_total")
            ctx.summary = self._FALLBACK
            ctx.summary_generated = False
            ctx.timed_out = True

    @staticmethod
    async def _generate(history: list[ChatMessage], llm: ILLM) -> str:
        messages = list(history) + [ChatMessage(role="user", content=_SUMMARY_PROMPT)]
        chunks: list[str] = []
        async for token in llm.generate(messages):
            chunks.append(token)
        return "".join(chunks).strip()


class PersistSummaryStep:
    """Persist the summary as a transcript_entries row."""
    name = "persist_session_summary"

    async def run(self, ctx: FinalizationContext) -> None:
        if ctx.transcripts is None or not ctx.summary:
            return
        ctx.transcripts.record_turn(ctx.session_id, "[session_summary]", 1.0, ctx.summary, False)
        ctx.transcript_written = True


class MetricsStep:
    """Emit finalize success/latency metrics."""
    name = "emit_metrics"

    async def run(self, ctx: FinalizationContext) -> None:
        ctx.metrics.increment("conversation_finalize_success_total")
        ctx.metrics.observe(
            "conversation_finalize_latency_ms", (time.monotonic() - ctx.started_at) * 1000.0,
        )


class TracingStep:
    """No-op placeholder."""
    name = "finish_tracing"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


class ProviderCleanupStep:
    """No-op: providers are shared per-process, not per-session."""
    name = "close_provider_sessions"

    async def run(self, ctx: FinalizationContext) -> None:
        return None


def _default_steps() -> list[IFinalizationStep]:
    return [
        StopRuntimeStep(),
        StopToolExecutorStep(),
        CancelTimersStep(),
        MemoryFlushStep(),
        SummaryStep(),
        PersistSummaryStep(),
        MetricsStep(),
        TracingStep(),
        ProviderCleanupStep(),
    ]


# ── Orchestrator ──────────────────────────────────────────────────────────────

class SessionFinalizer:
    """Shared across sessions; keeps only idempotency caches and pending early summaries.

    Only the summary starts early: running the full pipeline before success would tear down live state.
    """

    def __init__(
        self,
        transcripts: TranscriptBuilder | None = None,
        metrics:     IMetrics | None = None,
        steps:       list[IFinalizationStep] | None = None,
    ) -> None:
        self._transcripts = transcripts
        self._metrics     = metrics if metrics is not None else NullMetrics()
        self._steps       = steps if steps is not None else _default_steps()
        self._results:  dict[str, FinalizationResult]   = {}
        self._statuses: dict[str, FinalizationStatus]   = {}
        self._pending_summary_tasks: dict[str, asyncio.Task] = {}

    def start_summary_early(
        self, session_id: str, history: list[ChatMessage], llm: ILLM | None,
    ) -> None:
        """Start the summary speculatively on TransferInitiated; the task never raises.

        An existing task is kept; discard_pending_summary() must clear it first."""
        if session_id in self._pending_summary_tasks or not history or llm is None:
            return

        async def _generate_safe() -> str:
            try:
                return await SummaryStep._generate(history, llm)
            except Exception:
                log.exception(
                    "SessionFinalizer: speculative summary generation failed session=%s",
                    session_id,
                )
                return ""

        self._pending_summary_tasks[session_id] = asyncio.ensure_future(_generate_safe())

    def discard_pending_summary(self, session_id: str) -> None:
        """Cancel the speculative summary when its transfer failed or was cancelled."""
        task = self._pending_summary_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def finalize(
        self,
        session_id:   str,
        history:      list[ChatMessage],
        llm:          ILLM | None,
        cancel_event,
        reason:       str = "transfer_completed",
    ) -> FinalizationResult:
        if session_id in self._results:
            log.info("SessionFinalizer: already finalized session=%s — skipping", session_id)
            return self._results[session_id]

        self._statuses[session_id] = FinalizationStatus.RUNNING
        ctx = FinalizationContext(
            session_id=session_id, history=history, llm=llm,
            cancel_event=cancel_event, transcripts=self._transcripts, metrics=self._metrics,
            precomputed_summary_task=self._pending_summary_tasks.pop(session_id, None),
        )

        for step in self._steps:
            await self._run_step(step, ctx)

        status = FinalizationStatus.TIMED_OUT if ctx.timed_out else FinalizationStatus.COMPLETED
        result = FinalizationResult(
            summary=ctx.summary, summary_generated=ctx.summary_generated,
            transcript_written=ctx.transcript_written, status=status,
        )
        self._statuses[session_id] = status
        self._results[session_id] = result
        return result

    def status(self, session_id: str) -> FinalizationStatus | None:
        """None if finalize() was never called or was forgotten."""
        return self._statuses.get(session_id)

    def forget(self, session_id: str) -> None:
        """Drop the idempotency cache entry once the gateway has torn the call down."""
        self._results.pop(session_id, None)
        self._statuses.pop(session_id, None)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _run_step(self, step: IFinalizationStep, ctx: FinalizationContext) -> None:
        try:
            await step.run(ctx)
        except Exception:
            log.exception("SessionFinalizer: step '%s' failed session=%s", step.name, ctx.session_id)
