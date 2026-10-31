"""Per-(tenant, agent) concurrency and per-minute run caps, so egress can't be used as a flood relay.
In-process only: the effective ceiling is replicas × limit."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque

_MAX_CONCURRENT_ENV = "TOOLEXEC_MAX_CONCURRENT_RUNS_PER_AGENT"
_MAX_PER_MINUTE_ENV = "TOOLEXEC_MAX_RUNS_PER_MINUTE_PER_AGENT"
_MAX_WAIT_MS_ENV = "TOOLEXEC_ADMISSION_MAX_WAIT_MS"
_DEFAULT_MAX_CONCURRENT = 4
_DEFAULT_MAX_WAIT_MS = 1000
_DEFAULT_MAX_PER_MINUTE = 60
_WINDOW_SECONDS = 60.0

# (tenant_id, agent_id) -> count of runs currently held (acquired, not yet released)
_concurrent: dict[tuple[str, str], int] = defaultdict(int)
# (tenant_id, agent_id) -> timestamps of every acquire() admitted in the last minute
_run_timestamps: dict[tuple[str, str], list[float]] = defaultdict(list)


def _max_concurrent() -> int:
    return int(os.environ.get(_MAX_CONCURRENT_ENV, _DEFAULT_MAX_CONCURRENT))


def _max_per_minute() -> int:
    return int(os.environ.get(_MAX_PER_MINUTE_ENV, _DEFAULT_MAX_PER_MINUTE))


def max_wait_ms() -> int:
    """Longest a run queues for a concurrency slot before it is refused. 0 = refuse at once."""
    try:
        return max(0, int(os.environ.get(_MAX_WAIT_MS_ENV, _DEFAULT_MAX_WAIT_MS)))
    except ValueError:
        return _DEFAULT_MAX_WAIT_MS


# (tenant_id, agent_id) -> FIFO of futures for runs queued behind the concurrency cap
_waiters: dict[tuple[str, str], deque[asyncio.Future]] = defaultdict(deque)


def _sweep(key: tuple[str, str], now: float) -> None:
    timestamps = _run_timestamps[key]
    cutoff = now - _WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)


def _try_admit(key: tuple[str, str]) -> str:
    """'ok' (slot taken), 'concurrent' (transient: a slot may free soon) or 'per_minute' (hard)."""
    now = time.time()
    _sweep(key, now)
    if _concurrent[key] >= _max_concurrent():
        return "concurrent"
    if len(_run_timestamps[key]) >= _max_per_minute():
        return "per_minute"
    _concurrent[key] += 1
    _run_timestamps[key].append(now)
    return "ok"


def acquire(tenant_id: str, agent_id: str) -> bool:
    """True holds a slot (caller MUST release() in a finally when the run ends); False = refused.
    Must stay await-free so the check-and-increment can't interleave."""
    return _try_admit((tenant_id, agent_id)) == "ok"


def _hand_off(key: tuple[str, str]) -> bool:
    """Give a just-freed slot straight to the first live waiter (so nobody can barge in between).
    The slot stays counted in _concurrent. A waiter is refused instead if the per-minute cap is full."""
    waiters = _waiters.get(key)
    handed = False
    while waiters:
        fut = waiters.popleft()
        if fut.done():
            continue
        now = time.time()
        _sweep(key, now)
        if len(_run_timestamps[key]) >= _max_per_minute():
            fut.set_result(False)
            continue
        _run_timestamps[key].append(now)
        fut.set_result(True)
        handed = True
        break
    if waiters is not None and not waiters:
        _waiters.pop(key, None)
    return handed


async def acquire_wait(tenant_id: str, agent_id: str, max_wait_s: float) -> bool:
    """Like acquire(), but a run refused only by the concurrency cap queues (FIFO) for up to max_wait_s
    for a slot instead of failing at once. The per-minute cap is never waited on. Same release() contract."""
    key = (tenant_id, agent_id)
    # Per-minute is hard: refuse at once, even behind queued runs.
    _sweep(key, time.time())
    if len(_run_timestamps[key]) >= _max_per_minute():
        return False
    # Don't barge past runs already queued.
    outcome = "concurrent" if _waiters.get(key) else _try_admit(key)
    if outcome == "ok":
        return True
    if outcome == "per_minute" or max_wait_s <= 0:
        return False

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _waiters[key].append(fut)
    try:
        return bool(await asyncio.wait_for(fut, max_wait_s))
    except asyncio.TimeoutError:
        if fut.done() and not fut.cancelled() and fut.result():
            return True  # slot was handed over as the timer fired
        _discard(key, fut)
        return False
    except BaseException:
        # Cancelled (caller barged in). A slot handed to us but never used must go back.
        if fut.done() and not fut.cancelled() and fut.result():
            release(tenant_id, agent_id)
        else:
            _discard(key, fut)
        raise


def _discard(key: tuple[str, str], fut: asyncio.Future) -> None:
    waiters = _waiters.get(key)
    if waiters is not None:
        try:
            waiters.remove(fut)
        except ValueError:
            pass
        if not waiters:
            _waiters.pop(key, None)


def release(tenant_id: str, agent_id: str) -> None:
    key = (tenant_id, agent_id)
    if _concurrent[key] > 0:
        if not _hand_off(key):
            _concurrent[key] -= 1
