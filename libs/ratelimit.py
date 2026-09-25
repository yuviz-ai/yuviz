"""
FixedWindowCounter — extracted verbatim out of services/config/app.py so
a second service (Cloudonix) can reuse the same bounded-memory rate
counter instead of hand-rolling a new one and re-earning the fix this
class already has (lesson 25: a rate limiter whose recovery only holds
under a tuning constant nobody runs in production is not a fix).
"""

from __future__ import annotations

import time


class FixedWindowCounter:
    """Per-process, per-worker fixed-window rate counter — no Redis, resets
    on restart, and a multi-replica deployment multiplies every limit by
    the replica count (accepted: Config Service runs single-replica today,
    see design doc's throttle risk). `over_limit` only peeks; callers that
    need outcome-blind counting (the invite probe cap) call `increment`
    unconditionally themselves rather than relying on this class to do it
    for them on every check.

    `_buckets` entries are swept eventually, not just reset in place —
    without that, a key that's been reset in place but never deleted is a
    permanent dict entry, and AcceptThrottle keys on the client IP of two
    *public, unauthenticated* routes, so every distinct source address that
    ever hits them would otherwise leak one entry forever.

    Sweeping was originally a full O(n) scan of `_buckets` on *every*
    access — AcceptThrottle's `check()` alone calls into this four times a
    request (two `over_limit` + two `increment`), so with `hour`'s
    3600s window an attacker driving enough distinct source IPs turns the
    rate limiter itself into the bottleneck: it is bounded by "distinct
    IPs per hour", which is the attacker's own free variable, not by
    anything this process controls. Two changes fix that:

    - Below `_SWEEP_THRESHOLD` entries the scan is cheap, so it still runs
      on every access (unchanged behavior at ordinary traffic volumes).
      Above it, the sweep only runs every `_SWEEP_INTERVAL` accesses —
      amortizing the O(n) cost instead of paying it on every request once
      the map is already large. `_accesses_since_sweep` really does count
      accesses, not just mutations: `over_limit` (a non-mutating peek)
      calls `_maybe_sweep` too, and both `over_limit`/`increment` call it
      *before* the capacity check below, every time, regardless of whether
      that check ends up short-circuiting the rest of the call — sweeping
      must never be reachable only through the branch it exists to keep
      unstuck. (Earlier draft called `_maybe_sweep` from inside `_current`,
      which the capacity check `return`ed before reaching — once the map
      hit `_MAX_BUCKETS` nothing ever swept again, so a flood followed by a
      quiet period never recovered: every new key was refused forever
      instead of just until the next sweep evicted the stale ones.) This
      alone does not bound worst-case size: a flood of distinct keys
      arriving within a single window has nothing stale for the sweep to
      remove, no matter how often it runs.
    - `_MAX_BUCKETS` is a hard, explicit cap on distinct keys tracked at
      once. A key not already in `_buckets` is refused once the map is at
      capacity — fails closed (treated as already over limit / not
      recorded) rather than growing past the cap and letting the map,
      and the O(n) sweep cost with it, become unbounded. A capacity
      refusal always forces one extra `_evict_stale` and re-checks before
      actually refusing (`_refuse_new_key`) — without that, a flood that
      fills the map to capacity and then stops leaves every bucket stale
      but the periodic `_SWEEP_INTERVAL`-accesses sweep might not fire for
      another ~500 accesses, so ~499 legitimate requests on brand-new IPs
      would still get refused after the flood is long over. A request
      about to be denied is exactly the moment worth paying the O(n) scan
      for — it happens only when the map is actually full, not on every
      access."""

    _SWEEP_THRESHOLD = 1_000
    _SWEEP_INTERVAL = 500
    _MAX_BUCKETS = 20_000

    def __init__(self, *, limit: int, window_seconds: float):
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}
        self._accesses_since_sweep = 0

    def _evict_stale(self, now: float) -> None:
        stale = [k for k, (window_start, _) in self._buckets.items() if now - window_start >= self._window]
        for k in stale:
            del self._buckets[k]

    def _maybe_sweep(self, now: float) -> None:
        if len(self._buckets) <= self._SWEEP_THRESHOLD:
            self._accesses_since_sweep = 0
            self._evict_stale(now)
            return
        self._accesses_since_sweep += 1
        if self._accesses_since_sweep >= self._SWEEP_INTERVAL:
            self._accesses_since_sweep = 0
            self._evict_stale(now)

    def _at_capacity_for_new_key(self, key: str) -> bool:
        return key not in self._buckets and len(self._buckets) >= self._MAX_BUCKETS

    def _refuse_new_key(self, key: str, now: float) -> bool:
        """True means: don't admit this key. Forces one extra sweep before
        actually refusing — see the class docstring's `_MAX_BUCKETS`
        paragraph for why a denial is the one moment worth the O(n) cost
        regardless of `_SWEEP_INTERVAL`."""
        if not self._at_capacity_for_new_key(key):
            return False
        self._accesses_since_sweep = 0
        self._evict_stale(now)
        return self._at_capacity_for_new_key(key)

    def _current(self, key: str, now: float) -> tuple[float, int]:
        window_start, count = self._buckets.get(key, (now, 0))
        if now - window_start >= self._window:
            window_start, count = now, 0
        return window_start, count

    def over_limit(self, key: str) -> tuple[bool, int]:
        """Returns (over, retry_after_seconds). Does not increment."""
        now = time.monotonic()
        self._maybe_sweep(now)
        if self._refuse_new_key(key, now):
            return True, int(self._window)
        window_start, count = self._current(key, now)
        if count >= self._limit:
            return True, int(self._window - (now - window_start)) + 1
        return False, 0

    def increment(self, key: str) -> None:
        now = time.monotonic()
        self._maybe_sweep(now)
        if self._refuse_new_key(key, now):
            return
        window_start, count = self._current(key, now)
        self._buckets[key] = (window_start, count + 1)
