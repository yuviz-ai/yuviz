import math

from .. import tool_latency as tool_latency_module
from ..tool_latency import (
    _MAX_AGE_S,
    _MAX_KEYS_PER_TENANT,
    _MAX_TENANTS,
    _MIN_SAMPLES,
    _SWEEP_EVERY,
    _SWEEP_SCAN,
    _WINDOW,
    ToolLatencyStore,
)


def test_tenant_isolation():
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "book_appointment", 500.0)
    assert store.average_ms("t1", "a1", "book_appointment") == 500.0
    assert store.average_ms("t2", "a2", "book_appointment") is None


def test_agent_isolation():
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "book_appointment", 500.0)
    assert store.average_ms("t1", "a2", "book_appointment") is None


def test_window_caps_and_averages_retained_samples():
    store = ToolLatencyStore()
    for i in range(_WINDOW + 10):
        store.record("t1", "a1", "tool", float(i))
    avg = store.average_ms("t1", "a1", "tool")
    expected = sum(range(10, _WINDOW + 10)) / _WINDOW
    assert avg == expected


def test_average_none_below_min_samples():
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES - 1):
        store.record("t1", "a1", "tool", 500.0)
    assert store.average_ms("t1", "a1", "tool") is None


def test_average_none_after_only_invalid_records():
    store = ToolLatencyStore()
    for v in (0, -1, float("nan"), float("inf")):
        store.record("t1", "a1", "tool", v)
    assert store.average_ms("t1", "a1", "tool") is None


def test_age_out(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(tool_latency_module.time, "monotonic", lambda: clock["t"])
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "tool", 500.0)
    clock["t"] += _MAX_AGE_S + 1
    assert store.average_ms("t1", "a1", "tool") is None


def test_age_out_mixed_stale_and_fresh(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(tool_latency_module.time, "monotonic", lambda: clock["t"])
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "tool", 100.0)
    clock["t"] += _MAX_AGE_S + 1
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "tool", 500.0)
    assert store.average_ms("t1", "a1", "tool") == 500.0


def test_per_tenant_lru_eviction():
    store = ToolLatencyStore()
    for i in range(_MAX_KEYS_PER_TENANT + 1):
        store.record("t1", f"a{i}", "tool", 500.0)
    inner = store._tenants["t1"]
    assert len(inner) == _MAX_KEYS_PER_TENANT
    assert ("a0", "tool") not in inner
    assert ("a1", "tool") in inner


def test_lru_eviction_keeps_key_warm_via_reads():
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a0", "tool", 500.0)
    for i in range(1, _MAX_KEYS_PER_TENANT):
        store.record("t1", f"a{i}", "tool", 500.0)
    # a0 is now least-recently-inserted; a read should keep it warm.
    store.average_ms("t1", "a0", "tool")
    store.record("t1", "a_new", "tool", 500.0)
    inner = store._tenants["t1"]
    assert ("a0", "tool") in inner
    assert ("a1", "tool") not in inner


def test_tenant_dropped_from_outer_map_once_only_key_ages_out(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(tool_latency_module.time, "monotonic", lambda: clock["t"])
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "tool", 500.0)
    clock["t"] += _MAX_AGE_S + 1
    assert store.average_ms("t1", "a1", "tool") is None
    assert "t1" not in store._tenants


def test_outer_tenant_map_is_bounded():
    # A stream of unique tenant ids must not grow the outer map without
    # bound — record()'s O(1) cap, independent of _sweep()'s scan window.
    # Which specific tenant gets evicted can shift under _sweep()'s own
    # move_to_end bookkeeping (it also touches LRU order); the property
    # that must always hold is the size cap and that recent tenants survive.
    store = ToolLatencyStore()
    for i in range(_MAX_TENANTS + 5):
        store.record(f"t{i}", "a1", "tool", 500.0)
    assert len(store._tenants) == _MAX_TENANTS
    assert f"t{_MAX_TENANTS + 4}" in store._tenants
    assert f"t{_MAX_TENANTS + 3}" in store._tenants


def test_cross_tenant_eviction_immunity():
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "tool", 500.0)
    for i in range(_MAX_KEYS_PER_TENANT * 3):
        store.record("t2", f"a{i}", "tool", 500.0)
    assert store.average_ms("t1", "a1", "tool") == 500.0


def test_empty_identity_skip():
    store = ToolLatencyStore()
    for _ in range(_WINDOW):
        store.record("", "", "book_appointment", 400.0)
        store.record("t1", "", "book_appointment", 400.0)
        store.record("", "a1", "book_appointment", 400.0)
    assert store._tenants == {}
    assert store.average_ms("", "", "book_appointment") is None
    assert store.average_ms("t1", "", "book_appointment") is None
    assert store.average_ms("", "a1", "book_appointment") is None


def test_sweep_reclaims_write_only_aliases_never_read(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(tool_latency_module.time, "monotonic", lambda: clock["t"])
    store = ToolLatencyStore()
    for i in range(500):
        store.record(f"alias{i}", "a1", "tool", 500.0)

    clock["t"] += _MAX_AGE_S + 1
    needed = _SWEEP_EVERY * math.ceil(500 / _SWEEP_SCAN)
    for _ in range(needed):
        store.record("live", "a1", "tool", 500.0)

    assert len(store._tenants) == 1
    assert "live" in store._tenants


def test_sweep_never_reclaims_live_idle_tenant(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(tool_latency_module.time, "monotonic", lambda: clock["t"])
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "tool", 500.0)

    clock["t"] += _MAX_AGE_S / 2
    for i in range(2000):
        store.record(f"other{i}", "a1", "tool", 500.0)

    assert store.average_ms("t1", "a1", "tool") == 500.0


def test_sweep_keeps_partially_stale_tenant_whole(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(tool_latency_module.time, "monotonic", lambda: clock["t"])
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "expired_tool", 500.0)

    clock["t"] += _MAX_AGE_S + 1
    for _ in range(_MIN_SAMPLES):
        store.record("t1", "a1", "fresh_tool", 700.0)

    needed = _SWEEP_EVERY * math.ceil(1 / _SWEEP_SCAN) + 1
    for i in range(needed):
        store.record(f"filler{i}", "a1", "tool", 500.0)

    assert store.average_ms("t1", "a1", "fresh_tool") == 700.0
    assert store.average_ms("t1", "a1", "expired_tool") is None


def test_sweep_trigger_only_advances_on_accepted_records(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(tool_latency_module.time, "monotonic", lambda: clock["t"])
    store = ToolLatencyStore()
    for _ in range(_MIN_SAMPLES):
        store.record("stale", "a1", "tool", 500.0)
    clock["t"] += _MAX_AGE_S + 1

    for _ in range(_SWEEP_EVERY * 5):
        store.record("", "", "tool", 500.0)
        store.record("t1", "a1", "tool", -1.0)

    assert "stale" in store._tenants
