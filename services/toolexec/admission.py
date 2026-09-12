"""
services/toolexec/admission.py — per-(tenant_id, agent_id) concurrency and
per-minute run caps (finding 10): without this, an authenticated caller
could turn the platform's egress into a flood relay against a third party
with the platform holding the bill and the abuse complaint.

Enforced entirely in-process: two dicts, swept inline on every acquire()
call. Deliberately no Redis, no background sweep task, no executor — there
is nothing here with a lifecycle to tear down at shutdown (lesson 26). The
caps are therefore per-replica: the effective ceiling is replicas × limit,
which bounds rather than eliminates abuse and is stated as such, not
hidden (design Risks).
"""

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
    # Read fresh, not cached at import time, so an operator env-var change
    # takes effect without a restart racing this specific knob.
    return int(os.environ.get(_MAX_CONCURRENT_ENV, _DEFAULT_MAX_CONCURRENT))


def _max_per_minute() -> int:
    return int(os.environ.get(_MAX_PER_MINUTE_ENV, _DEFAULT_MAX_PER_MINUTE))


def _sweep(key: tuple[str, str], now: float) -> None:
    timestamps = _run_timestamps[key]
    cutoff = now - _WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)


def acquire(tenant_id: str, agent_id: str) -> bool:
    """Returns True (a slot is held; the caller MUST call release() in a
    `finally` once the run reaches a terminal status — including the
    barge-in case, where the chain keeps running server-side and so must
    keep holding its slot until it actually finishes) or False (refused;
    the caller takes no run row and makes no HTTP call). No `await`
    anywhere in this function — asyncio is cooperative, so there is no
    interleaving window for a second coroutine to race this check."""
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
