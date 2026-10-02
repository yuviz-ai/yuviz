"""Bounded-memory fixed-window rate counter shared across services."""

from __future__ import annotations

import time


class FixedWindowCounter:
    """Per-process fixed-window counter (no Redis; limits multiply per replica).

    `over_limit` only peeks. Keys can be attacker-chosen IPs, so stale buckets are
    swept (amortized above _SWEEP_THRESHOLD) and new keys fail closed at _MAX_BUCKETS.
    Sweeping runs before the capacity check so a full map can always recover."""

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
        """True = refuse this key. Sweeps once first: a denial is worth the O(n) scan."""
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
