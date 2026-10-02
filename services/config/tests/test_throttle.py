"""FixedWindowCounter memory bounds: it keys public routes by client IP, so its
map must not grow without limit."""

from __future__ import annotations

from unittest.mock import patch

from services.config.app import FixedWindowCounter


def test_stale_entries_are_evicted_not_retained_forever():
    counter = FixedWindowCounter(limit=100, window_seconds=1)

    with patch("services.config.app.time.monotonic", return_value=0.0):
        counter.increment("ip-1")
        assert len(counter._buckets) == 1

    # ip-1's window has expired, so it must be swept.
    with patch("services.config.app.time.monotonic", return_value=5.0):
        counter.increment("ip-2")
        assert "ip-1" not in counter._buckets
        assert len(counter._buckets) == 1


def test_map_size_does_not_grow_with_a_stream_of_expired_one_off_keys():
    counter = FixedWindowCounter(limit=10, window_seconds=1)

    for i in range(500):
        with patch("services.config.app.time.monotonic", return_value=float(i * 2)):
            counter.increment(f"ip-{i}")
        assert len(counter._buckets) == 1


def test_map_is_bounded_when_many_distinct_keys_arrive_within_one_window():
    # All keys live at once, so nothing is stale; overshoot the cap and assert `==` to pin it.
    counter = FixedWindowCounter(limit=10, window_seconds=3600)

    with patch("services.config.app.time.monotonic", return_value=0.0):
        for i in range(FixedWindowCounter._MAX_BUCKETS + 5_000):
            counter.increment(f"ip-{i}")

    assert len(counter._buckets) == FixedWindowCounter._MAX_BUCKETS


def test_limiter_recovers_after_a_flood_once_the_window_passes():
    # Uses the real _SWEEP_INTERVAL: recovery on the first access after the flood
    # proves the capacity path forces its own sweep.
    counter = FixedWindowCounter(limit=10, window_seconds=60)

    with patch("services.config.app.time.monotonic", return_value=0.0):
        for i in range(FixedWindowCounter._MAX_BUCKETS):
            counter.increment(f"ip-{i}")
        over, _ = counter.over_limit("brand-new-ip-during-flood")
    assert over is True
    assert len(counter._buckets) == FixedWindowCounter._MAX_BUCKETS

    # Window passed: the very first new IP must succeed immediately.
    with patch("services.config.app.time.monotonic", return_value=61.0):
        over, _ = counter.over_limit("very-first-new-ip-after-quiet-period")
        counter.increment("very-first-new-ip-after-quiet-period")
    assert over is False
    assert len(counter._buckets) == 1
    assert "very-first-new-ip-after-quiet-period" in counter._buckets


def test_new_key_is_rejected_once_map_is_at_capacity():
    # Fails closed: an unseen source at capacity is treated as over limit.
    counter = FixedWindowCounter(limit=1000, window_seconds=3600)
    counter._buckets = {f"ip-{i}": (0.0, 0) for i in range(FixedWindowCounter._MAX_BUCKETS)}

    with patch("services.config.app.time.monotonic", return_value=0.0):
        over, retry_after = counter.over_limit("brand-new-ip")
        counter.increment("brand-new-ip")

    assert over is True
    assert retry_after > 0
    assert "brand-new-ip" not in counter._buckets
    assert len(counter._buckets) == FixedWindowCounter._MAX_BUCKETS
