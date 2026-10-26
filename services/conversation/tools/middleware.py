"""Middleware chain wrapping any IToolExecutor (logging, metrics, circuit breaking, retry, timeout).

Order: Logging/Metrics outermost; CircuitBreaker outside Retry so an open breaker fails fast; Timeout innermost, per attempt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

from ..metrics import IMetrics, NullMetrics
from ..tool_latency import ToolLatencyStore
from .types import ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)

NextCall = Callable[[ToolExecutionRequest], Awaitable[ToolResult]]


def _redact(value: Any, redact_keys: frozenset[str]) -> Any:
    """Replace values of keys in redact_keys with "[redacted]" at any depth."""
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k in redact_keys else _redact(v, redact_keys))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, redact_keys) for v in value]
    return value


class LoggingMiddleware:
    """Logs tool calls; redact_arg_keys comes from the resolved policy's sensitive_arg_keys."""

    def __init__(self, redact_arg_keys: frozenset[str] = frozenset()) -> None:
        self._redact_arg_keys = redact_arg_keys

    async def __call__(self, request: ToolExecutionRequest, call_next: NextCall) -> ToolResult:
        log.info(
            "tool_call start tool=%s call_id=%s tenant=%s agent=%s arguments=%r",
            request.tool_name, request.tool_call_id,
            request.context.tenant_id, request.context.agent_id,
            _redact(request.arguments, self._redact_arg_keys),
        )
        result = await call_next(request)
        log.info(
            "tool_call done tool=%s call_id=%s status=%s payload=%r error=%r",
            request.tool_name, request.tool_call_id, result.status.value,
            _redact(result.payload, self._redact_arg_keys), result.error,
        )
        return result


class MetricsMiddleware:
    def __init__(self, metrics: IMetrics | None = None) -> None:
        self._metrics = metrics if metrics is not None else NullMetrics()

    async def __call__(self, request: ToolExecutionRequest, call_next: NextCall) -> ToolResult:
        t0 = time.monotonic()
        result = await call_next(request)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self._metrics.increment(f"tool_call_total.{request.tool_name}.{result.status.value}")
        self._metrics.observe(f"tool_call_latency_ms.{request.tool_name}", elapsed_ms)
        return result


class LatencyRecorderMiddleware:
    """Feeds ToolLatencyStore. UNAVAILABLE (breaker fail-fast) isn't recorded: no work happened,
    but failures and timeouts are, since callers genuinely wait that long."""

    def __init__(self, store: ToolLatencyStore) -> None:
        self._store = store

    async def __call__(self, request: ToolExecutionRequest, call_next: NextCall) -> ToolResult:
        t0 = time.monotonic()
        result = await call_next(request)
        if result.status != ToolStatus.UNAVAILABLE:
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._store.record(
                request.context.tenant_id, request.context.agent_id, request.tool_name, elapsed_ms,
            )
        return result


class BreakerBoard:
    """Consecutive-failure counting per key, not a rolling time window. Must outlive a single call:
    the orchestrator owns one so failures accumulate across calls (a chain built per call would never open)."""

    def __init__(self, fails_before_open: int = 5, cooldown_s: float = 30.0) -> None:
        self._fails_before_open = fails_before_open
        self._cooldown_s = cooldown_s
        self._consecutive_failures: dict[Any, int] = {}
        self._opened_at: dict[Any, float] = {}
        self._trial_started: dict[Any, float] = {}

    def admit(self, key: Any) -> str | tuple[str, float] | None:
        """"closed" = normal call, ("trial", token) = the single half-open probe, None = refuse (fail fast).
        After the cooldown exactly one caller is admitted as the trial; the rest keep failing fast until
        it resolves, so a still-broken service costs one step timeout per cycle, not one per caller."""
        opened_at = self._opened_at.get(key)
        if opened_at is None:
            return "closed"
        now = time.monotonic()
        if now - opened_at < self._cooldown_s:
            return None
        started = self._trial_started.get(key)
        if started is not None and now - started < self._cooldown_s:
            return None  # a trial is in flight (and a lost one is replaced after one more cooldown)
        self._trial_started[key] = now
        return ("trial", now)

    def _end_trial(self, key: Any, admission: Any) -> None:
        # Only the trial that owns the marker clears it: a replaced (stale) trial finishing late must not
        # clear the newer trial's marker and let a third caller in alongside it.
        if self._trial_started.get(key) == admission[1]:
            del self._trial_started[key]

    def record(self, key: Any, result: ToolResult, admission: Any = "closed") -> None:
        trial = isinstance(admission, tuple)
        if trial:
            self._end_trial(key, admission)
        if is_infrastructure_failure(result):
            count = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = count
            if count >= self._fails_before_open or trial:
                self._opened_at[key] = time.monotonic()  # (re-)open, restarting the cooldown
                log.warning("tool_call circuit_breaker_opened key=%s consecutive_failures=%d", key, count)
        elif result.status is not ToolStatus.CANCELLED:
            if key in self._opened_at and not trial:
                return  # a straggler that started before the breaker opened says nothing about now
            self._consecutive_failures[key] = 0
            self._opened_at.pop(key, None)  # the trial succeeded (or the service answered): close

    def abandon(self, key: Any, admission: Any) -> None:
        """A trial that never produced a result (cancelled, raised) must not wedge the breaker half-open."""
        if isinstance(admission, tuple):
            self._end_trial(key, admission)


# Errors a caller can cause (a wrong order number is a customer-API 404): they must never count, or a few
# callers could shut a tenant's API off for everyone.
_INFRA_FAILED_ERRORS = frozenset({
    "toolexec_unavailable", "provider_unavailable", "knowledge_unavailable", "local_tool_failed",
})
_CALLER_DRIVEN_UNAVAILABLE = frozenset({"send_cap_reached", "circuit_breaker_open"})
_HTTP_5XX = re.compile(r"http_status_5\d\d")


def is_infrastructure_failure(result: ToolResult) -> bool:
    """Timeouts, 5xx/transport errors and unavailability count against a breaker; 4xx, validation and
    business-rule failures mean the service answered."""
    if result.status is ToolStatus.TIMEOUT:
        return True
    if result.status is ToolStatus.UNAVAILABLE:
        return result.error not in _CALLER_DRIVEN_UNAVAILABLE
    if result.status is ToolStatus.FAILED:
        return bool(result.error) and (result.error in _INFRA_FAILED_ERRORS or bool(_HTTP_5XX.fullmatch(result.error)))
    return False


def breaker_key(request: ToolExecutionRequest) -> tuple[str, str, str]:
    """Per tenant and, for execute_api, per API: one tenant's dead CRM must not open the breaker for
    another tenant, nor for that tenant's other APIs."""
    return (
        request.context.tenant_id, request.tool_name, str(request.arguments.get("api_name") or ""),
    )


class CircuitBreakerMiddleware:
    def __init__(
        self, fails_before_open: int = 5, cooldown_s: float = 30.0, board: BreakerBoard | None = None,
    ) -> None:
        self._board = board if board is not None else BreakerBoard(fails_before_open, cooldown_s)

    async def __call__(self, request: ToolExecutionRequest, call_next: NextCall) -> ToolResult:
        key = breaker_key(request)
        admission = self._board.admit(key)
        if admission is None:
            log.warning("tool_call circuit_breaker_open tool=%s", request.tool_name)
            return ToolResult(status=ToolStatus.UNAVAILABLE, error="circuit_breaker_open")
        try:
            result = await call_next(request)
        except BaseException:
            self._board.abandon(key, admission)
            raise
        self._board.record(key, result, admission)
        return result


class RetryMiddleware:
    """Off by default (max_retries=0); each retry gets its own Timeout-bounded attempt, not an extended budget."""

    def __init__(self, max_retries: int = 0) -> None:
        self._max_retries = max_retries

    async def __call__(self, request: ToolExecutionRequest, call_next: NextCall) -> ToolResult:
        result = await call_next(request)
        attempts = 0
        while result.status == ToolStatus.FAILED and attempts < self._max_retries:
            attempts += 1
            log.info("tool_call retry tool=%s attempt=%d", request.tool_name, attempts)
            result = await call_next(request)
        return result


class TimeoutMiddleware:
    def __init__(self, timeout_ms: int = 6000) -> None:
        self._timeout_s = timeout_ms / 1000

    async def __call__(self, request: ToolExecutionRequest, call_next: NextCall) -> ToolResult:
        try:
            return await asyncio.wait_for(call_next(request), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            log.warning("tool_call timeout tool=%s timeout_s=%.1f", request.tool_name, self._timeout_s)
            return ToolResult(status=ToolStatus.TIMEOUT)


class MiddlewareChain:
    """Composes middlewares outermost-first around a bare IToolExecutor.execute."""

    def __init__(self, executor, middlewares: list) -> None:
        self._executor = executor
        self._middlewares = middlewares

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        async def call_executor(req: ToolExecutionRequest) -> ToolResult:
            return await self._executor.execute(req)

        chain: NextCall = call_executor
        for mw in reversed(self._middlewares):
            chain = _bind(mw, chain)
        return await chain(request)


def _bind(middleware, next_call: NextCall) -> NextCall:
    async def bound(request: ToolExecutionRequest) -> ToolResult:
        return await middleware(request, next_call)
    return bound


def build_default_chain(
    executor, timeout_ms: int = 6000, metrics: IMetrics | None = None,
    redact_arg_keys: frozenset[str] = frozenset(),
    latency_store: ToolLatencyStore | None = None,
    breaker_board: BreakerBoard | None = None,
) -> MiddlewareChain:
    """Standard chain: Logging, Metrics, Latency (if a store is given), CircuitBreaker, Retry, Timeout."""
    middlewares = [
        LoggingMiddleware(redact_arg_keys=redact_arg_keys),
        MetricsMiddleware(metrics),
    ]
    if latency_store is not None:
        middlewares.append(LatencyRecorderMiddleware(latency_store))
    middlewares += [
        CircuitBreakerMiddleware(board=breaker_board),
        RetryMiddleware(),
        TimeoutMiddleware(timeout_ms=timeout_ms),
    ]
    return MiddlewareChain(executor, middlewares)
