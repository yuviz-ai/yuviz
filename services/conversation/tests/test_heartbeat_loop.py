"""Heartbeat loop: a slow or down Postgres never kills it, and never makes this node reap its peers."""

from __future__ import annotations

import asyncio
import logging

import pytest

from services.conversation.__main__ import (
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_RETRY_BACKOFF_S,
    HEARTBEAT_STEP_TIMEOUT_S,
    INACTIVE_CALL_TIMEOUT_S,
    REAP_WINDOW_S,
    _bounded_step,
    _run_heartbeat_loop,
)

OUTAGE_S = 60.0
PEER_STARTS_AFTER_RECOVERY_S = 7.0


def test_constants_fit_the_reap_budget():
    # Worst case between two good writes: a 5 s timeout, a 2 s backoff, a 5 s timeout, then the interval.
    assert HEARTBEAT_STEP_TIMEOUT_S <= 5
    assert HEARTBEAT_RETRY_BACKOFF_S <= 2
    assert (2 * HEARTBEAT_STEP_TIMEOUT_S + HEARTBEAT_INTERVAL_S + HEARTBEAT_STEP_TIMEOUT_S
            <= 2 * HEARTBEAT_INTERVAL_S)


async def test_bounded_step_returns_true_on_success():
    async def ok():
        return None

    assert await _bounded_step("ok", ok()) is True


async def test_bounded_step_times_out_after_the_real_limit(caplog):
    # Fails if _bounded_step drops its wait_for: the never-set Event would hang this test forever.
    caplog.set_level(logging.WARNING)
    never = asyncio.Event()
    started = asyncio.get_running_loop().time()

    assert await _bounded_step("hang", never.wait()) is False

    assert asyncio.get_running_loop().time() - started == pytest.approx(HEARTBEAT_STEP_TIMEOUT_S, abs=0.5)
    timeouts = [r for r in caplog.records if r.levelno == logging.WARNING and "hang" in r.getMessage()]
    assert len(timeouts) == 1 and "timed out" in timeouts[0].getMessage()


async def test_bounded_step_is_not_stretched_by_work_that_resists_cancellation(caplog):
    # Fails if the bound awaits the cancelled work (asyncio.wait_for does): this one sleeps 3 s more.
    async def stubborn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(3)
            raise RuntimeError("late failure")

    started = asyncio.get_running_loop().time()

    assert await _bounded_step("stubborn", stubborn()) is False

    assert asyncio.get_running_loop().time() - started < HEARTBEAT_STEP_TIMEOUT_S + 0.5
    await asyncio.sleep(3.2)  # the abandoned task finishes; its exception is consumed, not raised


async def test_bounded_step_logs_exception_and_returns_false(caplog):
    async def boom():
        raise ConnectionRefusedError("db down")

    assert await _bounded_step("boom", boom()) is False

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info[1].args == ("db down",)


async def test_bounded_step_lets_cancellation_through():
    never = asyncio.Event()
    task = asyncio.create_task(_bounded_step("hang", never.wait()))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait({task}, timeout=0.5)
    assert task.cancelled()
class _VirtualClock:
    """Sleep advances time instantly; the test stops the loop by cancelling it from inside sleep."""

    def __init__(self, on_sleep):
        self.now = 0.0
        self.sleeps: list[tuple[float, float]] = []
        self._on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append((self.now, seconds))
        self.now += seconds
        await self._on_sleep(self)


class _FakeTranscripts:
    def __init__(self, vc: dict, recovery_at: float, reap_raises_from: float = 0.0):
        self._vc = vc
        self._recovery_at = recovery_at
        self._reap_raises_from = reap_raises_from
        self.writes: list[float] = []
        self.reaps: list[float] = []
        self.inactive_runs: list[float] = []
        self.failed_writes = 0
        self.reap_raised_at: float | None = None

    async def heartbeat(self):
        now = self._vc["v"].now
        if now < self._recovery_at:
            self.failed_writes += 1
            raise ConnectionRefusedError("connect to 10.0.0.5:5432 refused")
        self.writes.append(now)

    async def reconcile_dead_nodes(self, *, stale_after_seconds):
        now = self._vc["v"].now
        self.reaps.append(now)
        if self.reap_raised_at is None and now >= self._reap_raises_from:
            self.reap_raised_at = now
            raise ConnectionRefusedError("reap failed")

    async def reconcile_inactive_calls(self, *, inactive_after_seconds):
        self.inactive_runs.append(self._vc["v"].now)


async def test_outage_then_recovery_never_reaps_a_live_peer():
    # Fails if reaping runs on the first good write, the loop dies, or retry uses the 15 s interval.
    holder: dict = {}
    transcripts = _FakeTranscripts(holder, recovery_at=OUTAGE_S)
    peer_first_write = OUTAGE_S + PEER_STARTS_AFTER_RECOVERY_S

    async def on_sleep(vc):
        if vc.now > 400:
            raise asyncio.CancelledError  # ends the run; not a loop exit by its own choice

    vc = holder["v"] = _VirtualClock(on_sleep)

    with pytest.raises(asyncio.CancelledError):
        await _run_heartbeat_loop(transcripts, clock=vc.clock, sleep=vc.sleep)

    retry_gaps = {sec for at, sec in vc.sleeps if at < OUTAGE_S}
    assert retry_gaps == {HEARTBEAT_RETRY_BACKOFF_S}
    assert transcripts.failed_writes >= OUTAGE_S / HEARTBEAT_RETRY_BACKOFF_S - 1

    first_good = transcripts.writes[0]
    assert first_good - OUTAGE_S <= PEER_STARTS_AFTER_RECOVERY_S

    assert transcripts.reaps[0] - first_good >= REAP_WINDOW_S
    # The peer began writing at peer_first_write; every reap is long after, so it is never stale.
    peer_reaped = [t for t in transcripts.reaps if t < peer_first_write]
    assert peer_reaped == []

    # The loop kept going after reconcile_dead_nodes raised.
    assert len(transcripts.reaps) > 1

    # Calls whose transcript writes failed during the outage are not swept as inactive on recovery.
    assert transcripts.inactive_runs
    assert transcripts.inactive_runs[0] - first_good >= INACTIVE_CALL_TIMEOUT_S


async def test_a_hung_write_is_followed_by_a_good_one_within_the_reap_budget():
    # Real time: a hang shorter than a heartbeat gap must not leave the node silent past 7.5 s.
    class HangsOnce:
        def __init__(self):
            self.calls = 0
            self.hang_started = 0.0
            self.good_write_at: float | None = None

        async def heartbeat(self):
            loop_time = asyncio.get_running_loop().time()
            self.calls += 1
            if self.calls == 1:
                self.hang_started = loop_time
                await asyncio.Event().wait()
            self.good_write_at = loop_time

        async def reconcile_dead_nodes(self, *, stale_after_seconds):
            pass

        async def reconcile_inactive_calls(self, *, inactive_after_seconds):
            pass

    transcripts = HangsOnce()
    task = asyncio.create_task(_run_heartbeat_loop(transcripts))
    try:
        await asyncio.wait_for(_until(lambda: transcripts.good_write_at is not None), 10)
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=0.5)

    gap = transcripts.good_write_at - transcripts.hang_started
    assert gap >= HEARTBEAT_STEP_TIMEOUT_S  # the hang really was bounded by the step timeout
    assert gap <= 7.5


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.05)


async def test_a_fresh_node_waits_a_full_window_before_each_sweep():
    # A node booting as Postgres recovers has never failed a write, yet its peers may still be
    # recovering: it must not reap them, or sweep their calls, on its first iteration.
    holder: dict = {}
    transcripts = _FakeTranscripts(holder, recovery_at=0.0, reap_raises_from=INACTIVE_CALL_TIMEOUT_S)

    async def on_sleep(vc):
        if vc.now > INACTIVE_CALL_TIMEOUT_S + HEARTBEAT_INTERVAL_S:
            raise asyncio.CancelledError

    vc = holder["v"] = _VirtualClock(on_sleep)

    with pytest.raises(asyncio.CancelledError):
        await _run_heartbeat_loop(transcripts, clock=vc.clock, sleep=vc.sleep)

    assert transcripts.writes[0] == 0.0
    assert transcripts.reaps[0] == REAP_WINDOW_S
    assert transcripts.inactive_runs[0] == INACTIVE_CALL_TIMEOUT_S
    # The iteration where reconcile_dead_nodes raised still ran reconcile_inactive_calls.
    assert transcripts.reap_raised_at in transcripts.inactive_runs


async def test_cancel_during_a_failing_iteration_is_prompt():
    # Fails if CancelledError is swallowed anywhere on the failing path.
    class Hanging:
        async def heartbeat(self):
            await asyncio.Event().wait()

    task = asyncio.create_task(_run_heartbeat_loop(Hanging()))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait({task}, timeout=0.5)
    assert task.cancelled()
