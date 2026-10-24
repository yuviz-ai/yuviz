"""Retrieval time budget and per-dependency circuit breakers.

The numbers are the hot-path budget from the RAG redesign (#87): each stage has its own timeout and the whole
call has a hard deadline, so a slow embedder or database can never hold a live call. Every value is overridable
from the environment; RetrievalConfig is frozen so a request cannot see it change mid-flight."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .envparse import env_float, env_int

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RetrievalConfig:
    # Query-embedding budget. Local engines (ollama) keep 150 ms; hosted engines need a network round trip and
    # typically 100-400 ms, so they default to 300 ms. KNOWLEDGE_EMBED_TIMEOUT_MS overrides both; a provider
    # config's extra.embed_timeout_ms overrides that for one config (see resolve_embed_timeout_s).
    embed_timeout_s: float = 0.150
    hosted_embed_timeout_s: float = 0.300
    embed_timeout_override_s: float | None = None
    dense_timeout_s: float = 0.120
    lexical_timeout_s: float = 0.120
    rerank_timeout_s: float = 0.120
    deadline_s: float = 0.400
    # Once the lexical branch has answered with matches, wait at most this long for a dense branch that is still
    # embedding, then return the lexical results. A slow or unreachable embedding provider then costs one short grace
    # period per search instead of its whole budget. Off (None) by default: a hosted embedding that takes 200-300 ms
    # still joins the fusion. Turn it on (KNOWLEDGE_DENSE_GRACE_MS) where the embedding path is known to be slow;
    # every cut counts as a miss for the embedder's circuit breaker, so a provider that never makes the grace
    # ends up skipped altogether. 0 returns the moment lexical has matches.
    dense_grace_s: float | None = None
    # Breaker: consecutive failures before opening, then seconds before one half-open probe.
    breaker_failures: int = 5
    breaker_cooldown_s: float = 20.0
    # Close scores: top1 below `low_confidence_top` AND top1-top2 below `low_confidence_gap` => low_confidence.
    low_confidence_top: float = 0.5
    low_confidence_gap: float = 0.02
    # ts_rank_cd(...,32) is rank/(rank+1): one matching term in a ~12-word chunk scores ~0.09, so 0.05 keeps
    # single-term hits (SKUs, names) and only drops the near-zero ones.
    lexical_min_score: float = 0.05
    # A top hit supported only by lexical evidence is low_confidence unless it scores at least this much
    # (about three matching terms in a 12-word chunk). minimum_score applies to dense similarity only.
    lexical_confident_score: float = 0.3
    # Admission: in-flight retrievals per process, and per tenant. With conn=None a retrieval holds 2 pool
    # connections (a caller that keeps its own connection makes the search sequential: 0 extra). Global default =
    # max(2, (pool_max - 2) // 2) leaves 2 connections for plan loads, the log writer and admin traffic; the
    # per-tenant default is a third of that (min 1) so one tenant cannot take every slot.
    max_inflight: int = 9
    tenant_max_inflight: int = 3
    queue_wait_s: float = 0.150
    acquire_timeout_s: float = 0.060  # waiting longer than this for a pool connection means overload
    max_query_chars: int = 1000

    @classmethod
    def from_env(cls) -> "RetrievalConfig":
        d = cls()
        max_inflight = int(_env_float(
            "KNOWLEDGE_RETRIEVAL_MAX_INFLIGHT", max(2, (_env_float("KNOWLEDGE_DB_POOL_MAX", 20) - 2) // 2)))
        return cls(
            embed_timeout_override_s=_clamped_ms_or_none(os.environ.get("KNOWLEDGE_EMBED_TIMEOUT_MS")),
            dense_timeout_s=_env_float("KNOWLEDGE_DENSE_TIMEOUT_MS", d.dense_timeout_s * 1000) / 1000,
            lexical_timeout_s=_env_float("KNOWLEDGE_LEXICAL_TIMEOUT_MS", d.lexical_timeout_s * 1000) / 1000,
            rerank_timeout_s=_env_float("KNOWLEDGE_RERANK_TIMEOUT_MS", d.rerank_timeout_s * 1000) / 1000,
            deadline_s=_env_float("KNOWLEDGE_RETRIEVAL_DEADLINE_MS", d.deadline_s * 1000) / 1000,
            dense_grace_s=_clamped_grace_or_none(os.environ.get("KNOWLEDGE_DENSE_GRACE_MS")),
            breaker_failures=int(_env_float("KNOWLEDGE_BREAKER_FAILURES", d.breaker_failures)),
            breaker_cooldown_s=_env_float("KNOWLEDGE_BREAKER_COOLDOWN_S", d.breaker_cooldown_s),
            low_confidence_top=_env_float("KNOWLEDGE_LOW_CONFIDENCE_TOP", d.low_confidence_top),
            low_confidence_gap=_env_float("KNOWLEDGE_LOW_CONFIDENCE_GAP", d.low_confidence_gap),
            lexical_min_score=_env_float("KNOWLEDGE_LEXICAL_MIN_SCORE", d.lexical_min_score),
            lexical_confident_score=_env_float("KNOWLEDGE_LEXICAL_CONFIDENT_SCORE", d.lexical_confident_score),
            max_inflight=max_inflight,
            tenant_max_inflight=int(_env_float("KNOWLEDGE_RETRIEVAL_TENANT_MAX_INFLIGHT", max(1, max_inflight // 3))),
            queue_wait_s=_env_float("KNOWLEDGE_RETRIEVAL_QUEUE_WAIT_MS", d.queue_wait_s * 1000) / 1000,
            acquire_timeout_s=_env_float("KNOWLEDGE_POOL_ACQUIRE_TIMEOUT_MS", d.acquire_timeout_s * 1000) / 1000,
            max_query_chars=int(_env_float("KNOWLEDGE_RETRIEVAL_MAX_QUERY_CHARS", d.max_query_chars)),
        )


def _clamped_grace_or_none(raw: Any) -> float | None:
    """KNOWLEDGE_DENSE_GRACE_MS: blank or unusable = off (None); otherwise 0..2000 ms, in seconds."""
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        return None
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return None
    if ms != ms or ms in (float("inf"), float("-inf")):
        return None
    return min(max(ms, 0.0), 2000.0) / 1000


EMBED_TIMEOUT_MIN_MS, EMBED_TIMEOUT_MAX_MS = 50.0, 2000.0
LOCAL_EMBED_ENGINES = frozenset({"ollama"})


def _clamped_ms_or_none(raw: Any) -> float | None:
    """A timeout in milliseconds from untrusted-ish config (env, provider_configs.extra): number or numeric string
    in, seconds out, clamped to 50..2000 ms; anything else (bool, NaN, text, negative) is ignored."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return None
    if ms != ms or ms in (float("inf"), float("-inf")):
        return None
    return min(max(ms, EMBED_TIMEOUT_MIN_MS), EMBED_TIMEOUT_MAX_MS) / 1000


def resolve_embed_timeout_s(engine: str, extra: dict | None, cfg: RetrievalConfig) -> float:
    """Per-config extra.embed_timeout_ms > KNOWLEDGE_EMBED_TIMEOUT_MS > engine default (150 ms local, 300 ms hosted)."""
    per_config = _clamped_ms_or_none((extra or {}).get("embed_timeout_ms"))
    if per_config is not None:
        return per_config
    if cfg.embed_timeout_override_s is not None:
        return cfg.embed_timeout_override_s
    return cfg.embed_timeout_s if engine in LOCAL_EMBED_ENGINES else cfg.hosted_embed_timeout_s


class RetrievalUnavailable(RuntimeError):
    """Every retrieval branch failed or timed out and there is no prompt-mode content to fall back on.
    The router maps this to status='unavailable'; `degraded` and `trace_id` ride along for the response."""

    def __init__(self, message: str, *, degraded: list[str], trace_id: str) -> None:
        super().__init__(message)
        self.degraded = degraded
        self.trace_id = trace_id


class CircuitBreaker:
    """closed -> open after `failures` consecutive failures -> one half-open probe after `cooldown_s`.
    While open, allow() is a clock read: a dead dependency costs the hot path nothing."""

    def __init__(self, failures: int, cooldown_s: float, clock=time.monotonic, name: str = "") -> None:
        self.name = name  # an id (embedding_config_id) or "rerank": never tenant data or query text
        self._threshold = failures
        self._cooldown = cooldown_s
        self._clock = clock
        self._consecutive = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._cooldown and not self._probing:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self._cooldown and not self._probing:
            self._probing = True  # exactly one caller probes; the rest keep failing fast
            log.warning("circuit breaker half-open dependency=%s", self.name)
            return True
        return False

    def record_success(self) -> None:
        if self._opened_at is not None:
            log.warning("circuit breaker closed dependency=%s", self.name)
        self._consecutive = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._consecutive += 1
        if self._probing or self._consecutive >= self._threshold:
            if self._opened_at is None or self._probing:
                log.warning("circuit breaker opened dependency=%s consecutive_failures=%d", self.name, self._consecutive)
            self._opened_at = self._clock()
            self._probing = False

    def release_probe(self) -> None:
        """A cancelled probe says nothing about the dependency; let the next caller probe."""
        self._probing = False


class BreakerRegistry:
    """Breakers by key. Embedder keys carry the (tenant-owned) embedding_config_id, so one tenant's
    broken provider never opens the breaker for another tenant."""

    MAX_BREAKERS = 20000  # per-tenant keys exist (rerank:<tenant id>): bound the table, closed breakers go first

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, key: str, config: RetrievalConfig) -> CircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            if len(self._breakers) >= self.MAX_BREAKERS:
                for old in [k for k, b in self._breakers.items() if b.state == "closed"][: self.MAX_BREAKERS // 2]:
                    del self._breakers[old]
            breaker = CircuitBreaker(config.breaker_failures, config.breaker_cooldown_s, name=key)
            self._breakers[key] = breaker
        return breaker

    def clear(self) -> None:
        self._breakers.clear()


breakers = BreakerRegistry()


class PlatformValve:
    """A global safety valve for a platform-wide outage of a shared dependency (the rerank vendor). Unlike a
    per-tenant breaker it opens only when `tenants` DISTINCT tenants fail within `window_s`, so one tenant's own
    timeouts (a pathological agent, long documents) can never switch the feature off for everyone. Once open it lets
    one probe through after `cooldown_s`; a success closes it."""

    def __init__(self, tenants: int = 3, window_s: float = 30.0, cooldown_s: float = 20.0, clock=time.monotonic) -> None:
        self._tenants, self._window, self._cooldown, self._clock = tenants, window_s, cooldown_s, clock
        self._failures: dict[str, float] = {}
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self._cooldown and not self._probing else "open"

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self._cooldown and not self._probing:
            self._probing = True
            return True
        return False

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None
        self._probing = False

    def record_failure(self, tenant: str) -> None:
        now = self._clock()
        self._failures[tenant] = now
        for t in [t for t, at in self._failures.items() if now - at > self._window]:
            del self._failures[t]
        if self._probing or len(self._failures) >= self._tenants:
            self._opened_at, self._probing = now, False

    def release_probe(self) -> None:
        self._probing = False

    def clear(self) -> None:
        self.record_success()


rerank_valve = PlatformValve(
    tenants=env_int("KNOWLEDGE_RERANK_VALVE_TENANTS", 3, lo=2, hi=1000),
    window_s=env_float("KNOWLEDGE_RERANK_VALVE_WINDOW_S", 30.0, lo=1, hi=3600),
    cooldown_s=env_float("KNOWLEDGE_RERANK_VALVE_COOLDOWN_S", 20.0, lo=1, hi=3600),
)


class PoolOverloaded(RuntimeError):
    """No pooled connection became free within acquire_timeout_s: load, not a broken dependency."""


class BoundedPool:
    """Pool facade for tenant_conn(): acquire() gives up after `timeout_s` and raises PoolOverloaded, so a
    saturated pool is reported as overload instead of surfacing as a branch that "timed out"."""

    def __init__(self, pool: Any, timeout_s: float) -> None:
        self._pool = pool
        self._timeout_s = timeout_s

    def acquire(self) -> "_BoundedAcquire":
        return _BoundedAcquire(self._pool, self._timeout_s)


class _BoundedAcquire:
    def __init__(self, pool: Any, timeout_s: float) -> None:
        self._cm = pool.acquire(timeout=timeout_s)

    async def __aenter__(self) -> Any:
        try:
            return await self._cm.__aenter__()
        except asyncio.TimeoutError as exc:
            raise PoolOverloaded("database pool exhausted") from exc

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._cm.__aexit__(*exc_info)


@dataclass(eq=False)
class _Waiter:
    key: str
    max_inflight: int
    tenant_max: int
    fut: "asyncio.Future[bool]"
    granted: bool = False


class AdmissionLimiter:
    """In-process cap on concurrent retrievals, overall and per tenant, with a short bounded wait.
    It protects the DB pool: past the cap a caller gets a typed 'overloaded' answer quickly instead of every
    request fighting for connections and timing out.

    Admission: a new arrival is admitted at once whenever a global slot is free and its own tenant is under its
    cap, whatever other tenants have queued; a tenant at its cap queues only itself. Waiters queue per tenant and
    freed slots are handed out round-robin across tenants, so one tenant's backlog cannot starve the others.

    Ownership: a slot has exactly one owner at every instant. A grant (counters incremented, waiter.granted set,
    future resolved) transfers ownership to the waiter; if the waiter is cancelled or fails after that, the
    except-path in acquire() returns the slot. Nothing here relies on wait_for/shield semantics, which differ
    between Python 3.11 and 3.12+."""

    def __init__(self) -> None:
        self._total = 0
        self._per: dict[str, int] = {}
        self._queues: dict[str, deque[_Waiter]] = {}
        self._order: deque[str] = deque()  # tenants with a queue, in round-robin order

    def _fits(self, key: str, max_inflight: int, tenant_max: int) -> bool:
        return self._total < max_inflight and self._per.get(key, 0) < tenant_max

    def _take(self, key: str) -> None:
        self._total += 1
        self._per[key] = self._per.get(key, 0) + 1

    async def acquire(self, key: str, cfg: RetrievalConfig, wait_s: float) -> bool:
        if self._fits(key, cfg.max_inflight, cfg.tenant_max_inflight):
            self._take(key)
            return True
        if wait_s <= 0:
            return False
        loop = asyncio.get_running_loop()
        waiter = _Waiter(key, cfg.max_inflight, cfg.tenant_max_inflight, loop.create_future())
        if key not in self._queues:
            self._queues[key] = deque()
            self._order.append(key)
        self._queues[key].append(waiter)
        timer = loop.call_later(wait_s, self._expire, waiter)
        try:
            return await waiter.fut
        except BaseException:  # cancelled (or anything else) while queued or just granted
            if waiter.granted:
                self.release(key)  # the slot was handed over before we could use it: give it back
            raise
        finally:
            timer.cancel()
            self._discard(waiter)

    def _expire(self, waiter: _Waiter) -> None:
        if not waiter.fut.done():
            waiter.fut.set_result(False)
            self._discard(waiter)

    def _discard(self, waiter: _Waiter) -> None:
        queue = self._queues.get(waiter.key)
        if queue is not None:
            try:
                queue.remove(waiter)
            except ValueError:
                pass
            if not queue:
                del self._queues[waiter.key]
                try:
                    self._order.remove(waiter.key)
                except ValueError:
                    pass

    def release(self, key: str) -> None:
        self._total -= 1
        left = self._per.get(key, 1) - 1
        if left <= 0:
            self._per.pop(key, None)
        else:
            self._per[key] = left
        self._wake()

    def _wake(self) -> None:
        """Hand freed slots to queued waiters, round-robin across tenants, skipping tenants at their cap."""
        while self._order:
            granted = False
            for key in list(self._order):
                queue = self._queues.get(key)
                while queue and queue[0].fut.done():  # cancelled or expired while queued
                    queue.popleft()
                if not queue:
                    self._queues.pop(key, None)
                    self._order.remove(key)
                    continue
                head = queue[0]
                if self._fits(key, head.max_inflight, head.tenant_max):
                    queue.popleft()
                    if not queue:
                        del self._queues[key]
                        self._order.remove(key)
                    else:
                        self._order.remove(key)
                        self._order.append(key)  # next freed slot goes to the next tenant
                    self._take(key)
                    head.granted = True
                    head.fut.set_result(True)
                    granted = True
                    break
            if not granted:
                return

    @property
    def in_flight(self) -> int:
        return self._total


admission = AdmissionLimiter()
