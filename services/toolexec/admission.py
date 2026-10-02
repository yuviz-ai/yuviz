"""Per-(tenant, agent) concurrency and per-minute run caps, so egress can't be used as a flood relay.
In-process only: the effective ceiling is replicas × limit."""

from __future__ import annotations

import os
import time
from collections import defaultdict

_MAX_CONCURRENT_ENV = "TOOLEXEC_MAX_CONCURRENT_RUNS_PER_AGENT"
_MAX_PER_MINUTE_ENV = "TOOLEXEC_MAX_RUNS_PER_MINUTE_PER_AGENT"
_DEFAULT_MAX_CONCURRENT = 4
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


def _sweep(key: tuple[str, str], now: float) -> None:
    timestamps = _run_timestamps[key]
    cutoff = now - _WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)


def acquire(tenant_id: str, agent_id: str) -> bool:
    """True holds a slot (caller MUST release() in a finally when the run ends); False = refused.
    Must stay await-free so the check-and-increment can't interleave."""
    key = (tenant_id, agent_id)
    now = time.time()
    _sweep(key, now)

    if _concurrent[key] >= _max_concurrent():
        return False
    if len(_run_timestamps[key]) >= _max_per_minute():
        return False

    _concurrent[key] += 1
    _run_timestamps[key].append(now)
    return True


def release(tenant_id: str, agent_id: str) -> None:
    key = (tenant_id, agent_id)
    if _concurrent[key] > 0:
        _concurrent[key] -= 1
