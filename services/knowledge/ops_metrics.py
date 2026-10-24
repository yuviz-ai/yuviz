"""Process-level counters for the retrieval hot path, read by GET /internal/ops.

Plain integers bumped on the event loop (one thread, so no lock). Deliberately no tenant label, no query text and
no ids: this is "is the process healthy" data, safe to show to an operator and to scrape. Per-tenant numbers live
in retrieval_logs behind RLS. Counters reset on restart; rate() in Prometheus handles that."""

from __future__ import annotations

from typing import Any

from . import retrieval_log
from .resilience import admission, breakers

_counters: dict[str, int] = {"retrieval_overloaded": 0, "plan_cache_hit": 0, "plan_cache_miss": 0,
             "rerank_calls": 0, "rerank_decisive_skip": 0, "dense_grace_cut": 0}


def inc(name: str) -> None:
    _counters[name] += 1


def reset() -> None:
    for key in _counters:
        _counters[key] = 0


def snapshot() -> dict[str, Any]:
    states: dict[str, dict[str, int]] = {}
    for key, breaker in list(breakers._breakers.items()):
        kind = key.split(":", 1)[0]  # "embed:<config id>" -> "embed"; the id never leaves the process
        bucket = states.setdefault(kind, {"closed": 0, "open": 0, "half_open": 0})
        bucket[breaker.state] += 1
    return {
        **_counters,
        "retrieval_in_flight": admission.in_flight,
        "retrieval_logs_dropped_queue_full": retrieval_log.dropped,
        "retrieval_logs_dropped_rate_limited": retrieval_log.dropped_rate_limited,
        "breakers": states,
    }


def prometheus_text() -> str:
    snap = snapshot()
    lines: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[tuple[str, int]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{labels} {value}" for labels, value in samples)

    metric("knowledge_retrieval_overloaded_total", "counter", "Retrievals refused by admission or pool overload.",
           [("", snap["retrieval_overloaded"])])
    metric("knowledge_plan_cache_total", "counter", "Retrieval plan cache lookups.",
           [('{result="hit"}', snap["plan_cache_hit"]), ('{result="miss"}', snap["plan_cache_miss"])])
    metric("knowledge_dense_grace_cut_total", "counter",
           "Searches that returned lexical matches without waiting out a slow dense branch.",
           [("", snap["dense_grace_cut"])])
    metric("knowledge_retrieval_in_flight", "gauge", "Retrievals currently admitted.", [("", snap["retrieval_in_flight"])])
    metric("knowledge_retrieval_logs_dropped_total", "counter", "retrieval_logs rows dropped before insert.",
           [('{reason="queue_full"}', snap["retrieval_logs_dropped_queue_full"]),
            ('{reason="rate_limited"}', snap["retrieval_logs_dropped_rate_limited"])])
    samples = [(f'{{dependency="{kind}",state="{state}"}}', n)
               for kind, by_state in sorted(snap["breakers"].items()) for state, n in by_state.items()]
    metric("knowledge_breakers", "gauge", "Circuit breakers by dependency kind and state.", samples)
    return "\n".join(lines) + "\n"
