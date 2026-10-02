"""Tests for SessionFinalizer (graceful session finalization)."""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import MagicMock

from ..providers.interfaces import ChatMessage
from ..session_finalizer import (
    FinalizationStatus,
    MetricsStep,
    PersistSummaryStep,
    SessionFinalizer,
    SummaryStep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeMetrics:
    def __init__(self) -> None:
        self.increments: list[tuple[str, float]] = []
        self.observations: list[tuple[str, float]] = []

    def increment(self, name: str, value: float = 1.0) -> None:
        self.increments.append((name, value))

    def observe(self, name: str, value: float) -> None:
        self.observations.append((name, value))

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.increments if n == name)


def _make_llm(tokens: list[str]):
    llm = MagicMock()

    async def _gen(messages):
        for t in tokens:
            yield t

    llm.generate = _gen
    return llm


def _history() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="I need to talk to billing"),
        ChatMessage(role="assistant", content="Let me connect you."),
    ]


# ---------------------------------------------------------------------------
# Successful cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_generates_and_returns_summary():
    llm = _make_llm(["Caller wanted billing help; ", "transferred successfully."])
    finalizer = SessionFinalizer(transcripts=None, metrics=FakeMetrics())

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary == "Caller wanted billing help; transferred successfully."
    assert result.summary_generated is True
    assert result.status == FinalizationStatus.COMPLETED


@pytest.mark.asyncio
async def test_finalize_summary_prompt_never_addresses_caller_and_uses_history():
    """Verifies AgentRuntime (the LLM call) actually receives the real
    conversation history, not a hardcoded summary."""
    captured = []

    async def capturing_gen(messages):
        captured.append(list(messages))
        yield "summary text"

    llm = MagicMock()
    llm.generate = capturing_gen
    finalizer = SessionFinalizer()

    history = _history()
    await finalizer.finalize("s1", history, llm, asyncio.Event())

    assert len(captured) == 1
    sent = captured[0]
    assert sent[:2] == history  # original history passed through untouched
    assert sent[-1].role == "user"
    assert "summarize" in sent[-1].content.lower()


@pytest.mark.asyncio
async def test_finalize_with_no_history_returns_empty_summary():
    llm = _make_llm(["should not be reached"])
    finalizer = SessionFinalizer()

    result = await finalizer.finalize("s1", [], llm, asyncio.Event())

    assert result.summary == ""
    assert result.summary_generated is False


@pytest.mark.asyncio
async def test_finalize_with_no_llm_returns_empty_summary():
    finalizer = SessionFinalizer()

    result = await finalizer.finalize("s1", _history(), None, asyncio.Event())

    assert result.summary == ""


@pytest.mark.asyncio
async def test_finalize_stops_agent_runtime_by_setting_cancel_event():
    llm = _make_llm(["summary"])
    finalizer = SessionFinalizer()
    cancel_event = asyncio.Event()
    assert not cancel_event.is_set()

    await finalizer.finalize("s1", _history(), llm, cancel_event)

    assert cancel_event.is_set()


@pytest.mark.asyncio
async def test_finalize_tolerates_missing_cancel_event():
    llm = _make_llm(["summary"])
    finalizer = SessionFinalizer()

    result = await finalizer.finalize("s1", _history(), llm, None)

    assert result.summary == "summary"


# ---------------------------------------------------------------------------
# Summary timeout: LLM latency must never hold up cleanup or the ConversationFinalized ack.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_timeout_uses_fallback_and_does_not_block():
    async def slow_gen(messages):
        await asyncio.sleep(10)
        yield "too slow"  # pragma: no cover - never reached

    llm = MagicMock()
    llm.generate = slow_gen
    metrics = FakeMetrics()
    finalizer = SessionFinalizer(metrics=metrics, steps=[SummaryStep(timeout_s=0.05), MetricsStep()])

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.status == FinalizationStatus.TIMED_OUT
    assert result.summary_generated is False
    assert result.summary  # fallback text, not empty
    assert metrics.count("conversation_finalize_timeout_total") == 1
    # Cleanup still completes and still emits its normal success metric —
    # a slow LLM degrades the summary, not the whole finalization.
    assert metrics.count("conversation_finalize_success_total") == 1


@pytest.mark.asyncio
async def test_summary_timeout_fallback_gets_persisted():
    async def slow_gen(messages):
        await asyncio.sleep(10)
        yield "too slow"  # pragma: no cover

    llm = MagicMock()
    llm.generate = slow_gen
    transcripts = MagicMock()
    finalizer = SessionFinalizer(
        transcripts=transcripts,
        steps=[SummaryStep(timeout_s=0.05), PersistSummaryStep()],
    )

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.transcript_written is True
    transcripts.record_turn.assert_called_once()
    assert "unavailable" in transcripts.record_turn.call_args.args[3].lower()


# ---------------------------------------------------------------------------
# Memory persistence / transcript generation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_persists_summary_via_transcripts():
    llm = _make_llm(["Resolved billing question."])
    transcripts = MagicMock()
    finalizer = SessionFinalizer(transcripts=transcripts)

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    transcripts.record_turn.assert_called_once_with(
        "s1", "[session_summary]", 1.0, "Resolved billing question.", False,
    )
    assert result.transcript_written is True


@pytest.mark.asyncio
async def test_finalize_does_not_persist_empty_summary():
    finalizer_transcripts = MagicMock()
    finalizer = SessionFinalizer(transcripts=finalizer_transcripts)

    result = await finalizer.finalize("s1", [], None, asyncio.Event())  # -> empty summary

    finalizer_transcripts.record_turn.assert_not_called()
    assert result.transcript_written is False


@pytest.mark.asyncio
async def test_finalize_without_transcripts_configured_is_safe():
    llm = _make_llm(["summary"])
    finalizer = SessionFinalizer(transcripts=None)

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary == "summary"  # no exception, no persistence attempted
    assert result.transcript_written is False


# ---------------------------------------------------------------------------
# Metrics emission
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_emits_final_metrics():
    llm = _make_llm(["summary"])
    metrics = FakeMetrics()
    finalizer = SessionFinalizer(metrics=metrics)

    await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert metrics.count("conversation_finalize_success_total") == 1
    assert len(metrics.observations) == 1
    name, value = metrics.observations[0]
    assert name == "conversation_finalize_latency_ms"
    assert value >= 0.0


@pytest.mark.asyncio
async def test_finalize_emits_metrics_even_if_summary_generation_fails():
    """Requirement: cleanup failure handling — a bad step (the LLM call
    raising) must not prevent later steps (metrics) from running."""
    async def broken_gen(messages):
        raise RuntimeError("LLM exploded")
        yield  # pragma: no cover - unreachable, satisfies async generator syntax

    llm = MagicMock()
    llm.generate = broken_gen
    metrics = FakeMetrics()
    finalizer = SessionFinalizer(metrics=metrics)

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary == ""  # failed step degrades to empty, doesn't raise
    assert metrics.count("conversation_finalize_success_total") == 1


# ---------------------------------------------------------------------------
# Cleanup failure handling (individual step failures don't cascade)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_survives_transcript_persistence_failure():
    llm = _make_llm(["summary"])
    transcripts = MagicMock()
    transcripts.record_turn.side_effect = RuntimeError("DB down")
    metrics = FakeMetrics()
    finalizer = SessionFinalizer(transcripts=transcripts, metrics=metrics)

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary == "summary"  # returned despite the persistence failure
    assert result.transcript_written is False  # the failed step's own flag never set
    assert metrics.count("conversation_finalize_success_total") == 1  # later steps still ran


@pytest.mark.asyncio
async def test_finalize_never_raises_regardless_of_step_failures():
    async def broken_gen(messages):
        raise ValueError("boom")
        yield  # pragma: no cover

    llm = MagicMock()
    llm.generate = broken_gen
    transcripts = MagicMock()
    transcripts.record_turn.side_effect = RuntimeError("also broken")

    finalizer = SessionFinalizer(transcripts=transcripts)

    # Must not raise.
    await finalizer.finalize("s1", _history(), llm, asyncio.Event())


# ---------------------------------------------------------------------------
# Idempotent finalization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_is_idempotent_for_same_session():
    call_count = 0

    async def counting_gen(messages):
        nonlocal call_count
        call_count += 1
        yield f"summary #{call_count}"

    llm = MagicMock()
    llm.generate = counting_gen
    metrics = FakeMetrics()
    finalizer = SessionFinalizer(metrics=metrics)

    first = await finalizer.finalize("s1", _history(), llm, asyncio.Event())
    second = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert first.summary == "summary #1"
    assert second.summary == "summary #1"  # cached, not regenerated
    assert second is first  # literally the same cached result object
    assert call_count == 1
    assert metrics.count("conversation_finalize_success_total") == 1  # not double-counted


@pytest.mark.asyncio
async def test_finalize_is_independent_per_session():
    llm_a = _make_llm(["summary A"])
    llm_b = _make_llm(["summary B"])
    finalizer = SessionFinalizer()

    result_a = await finalizer.finalize("session-a", _history(), llm_a, asyncio.Event())
    result_b = await finalizer.finalize("session-b", _history(), llm_b, asyncio.Event())

    assert result_a.summary == "summary A"
    assert result_b.summary == "summary B"


@pytest.mark.asyncio
async def test_forget_clears_idempotency_cache():
    call_count = 0

    async def counting_gen(messages):
        nonlocal call_count
        call_count += 1
        yield f"summary #{call_count}"

    llm = MagicMock()
    llm.generate = counting_gen
    finalizer = SessionFinalizer()

    await finalizer.finalize("s1", _history(), llm, asyncio.Event())
    finalizer.forget("s1")
    second = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert call_count == 2
    assert second.summary == "summary #2"


# ---------------------------------------------------------------------------
# Status tracking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_is_running_then_completed():
    llm = _make_llm(["summary"])
    finalizer = SessionFinalizer()

    assert finalizer.status("s1") is None  # never started

    await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert finalizer.status("s1") == FinalizationStatus.COMPLETED


@pytest.mark.asyncio
async def test_status_is_timed_out_after_summary_timeout():
    async def slow_gen(messages):
        await asyncio.sleep(10)
        yield "too slow"  # pragma: no cover

    llm = MagicMock()
    llm.generate = slow_gen
    finalizer = SessionFinalizer(steps=[SummaryStep(timeout_s=0.05)])

    await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert finalizer.status("s1") == FinalizationStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_forget_clears_status_too():
    llm = _make_llm(["summary"])
    finalizer = SessionFinalizer()

    await finalizer.finalize("s1", _history(), llm, asyncio.Event())
    finalizer.forget("s1")

    assert finalizer.status("s1") is None


# ---------------------------------------------------------------------------
# Provider shutdown / tracing / tool executor — honest no-ops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_completes_with_no_transcripts_and_no_metrics_configured():
    """With every optional dependency absent, finalize still completes without raising."""
    llm = _make_llm(["fine"])
    finalizer = SessionFinalizer(transcripts=None, metrics=None)

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary == "fine"


# ---------------------------------------------------------------------------
# Step-pipeline extensibility
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_custom_steps_list_replaces_defaults():
    """A custom steps list replaces the defaults."""
    calls: list[str] = []

    class RecordingStep:
        name = "recording_step"

        async def run(self, ctx):
            calls.append(ctx.session_id)

    finalizer = SessionFinalizer(steps=[RecordingStep()])

    await finalizer.finalize("s1", _history(), None, asyncio.Event())

    assert calls == ["s1"]


@pytest.mark.asyncio
async def test_one_custom_step_failing_does_not_stop_later_custom_steps():
    calls: list[str] = []

    class BrokenStep:
        name = "broken_step"

        async def run(self, ctx):
            raise RuntimeError("custom step exploded")

    class RecordingStep:
        name = "recording_step"

        async def run(self, ctx):
            calls.append("ran")

    finalizer = SessionFinalizer(steps=[BrokenStep(), RecordingStep()])

    await finalizer.finalize("s1", _history(), None, asyncio.Event())

    assert calls == ["ran"]


# ---------------------------------------------------------------------------
# Speculative summary: start_summary_early() overlaps the summary LLM call with the transfer.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_uses_precomputed_summary_started_early():
    call_count = 0

    async def counting_gen(messages):
        nonlocal call_count
        call_count += 1
        yield "speculative summary"

    llm = MagicMock()
    llm.generate = counting_gen
    finalizer = SessionFinalizer()

    finalizer.start_summary_early("s1", _history(), llm)
    await asyncio.sleep(0)  # let the speculative task actually run

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary == "speculative summary"
    assert result.summary_generated is True
    assert call_count == 1  # not regenerated inside finalize()


@pytest.mark.asyncio
async def test_finalize_awaits_precomputed_summary_still_in_flight():
    """finalize() awaits a speculative summary that's still in flight."""
    async def slow_gen(messages):
        await asyncio.sleep(0.02)
        yield "eventually ready"

    llm = MagicMock()
    llm.generate = slow_gen
    finalizer = SessionFinalizer()

    finalizer.start_summary_early("s1", _history(), llm)
    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary == "eventually ready"
    assert result.summary_generated is True


@pytest.mark.asyncio
async def test_discard_pending_summary_prevents_reuse_by_a_later_finalize():
    call_count = 0

    async def counting_gen(messages):
        nonlocal call_count
        call_count += 1
        yield f"summary #{call_count}"

    llm = MagicMock()
    llm.generate = counting_gen
    finalizer = SessionFinalizer()

    finalizer.start_summary_early("s1", _history(), llm)
    finalizer.discard_pending_summary("s1")  # transfer failed before completing

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    # finalize() generated its own summary fresh — the discarded speculative
    # task was cancelled before it ever ran, so this is the only generation.
    assert result.summary == "summary #1"
    assert call_count == 1


def test_discard_pending_summary_is_safe_with_nothing_pending():
    finalizer = SessionFinalizer()
    finalizer.discard_pending_summary("never-started")  # must not raise


@pytest.mark.asyncio
async def test_start_summary_early_is_a_noop_with_no_history_or_llm():
    finalizer = SessionFinalizer()

    finalizer.start_summary_early("s1", [], MagicMock())
    finalizer.start_summary_early("s2", _history(), None)

    # Neither call should have registered a pending task — finalize() falls
    # through to its own (also-empty) summary path rather than hanging.
    result1 = await finalizer.finalize("s1", [], None, asyncio.Event())
    result2 = await finalizer.finalize("s2", _history(), None, asyncio.Event())
    assert result1.summary == ""
    assert result2.summary == ""


@pytest.mark.asyncio
async def test_speculative_summary_failure_falls_back_without_raising():
    async def broken_gen(messages):
        raise RuntimeError("LLM exploded")
        yield  # pragma: no cover - unreachable

    llm = MagicMock()
    llm.generate = broken_gen
    finalizer = SessionFinalizer()

    finalizer.start_summary_early("s1", _history(), llm)
    await asyncio.sleep(0)

    result = await finalizer.finalize("s1", _history(), llm, asyncio.Event())

    assert result.summary_generated is False
