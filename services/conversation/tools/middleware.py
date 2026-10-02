"""Middleware chain wrapping any IToolExecutor (logging, metrics, circuit breaking, retry, timeout).

Order: Logging/Metrics outermost; CircuitBreaker outside Retry so an open breaker fails fast; Timeout innermost, per attempt.
"""

from __future__ import annotations

import asyncio
import logging
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


class CircuitBreakerMiddleware:
    """Consecutive-failure counting, not a rolling time window."""

    def __init__(self, fails_before_open: int = 5, cooldown_s: float = 30.0) -> None:
        self._fails_before_open = fails_before_open
        self._cooldown_s = cooldown_s
        self._consecutive_failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def _breaker_key(self, request: ToolExecutionRequest) -> str:
        return request.tool_name

    async def __call__(self, request: ToolExecutionRequest, call_next: NextCall) -> ToolResult:
        key = self._breaker_key(request)
        opened_at = self._opened_at.get(key)
        if opened_at is not None:
            if time.monotonic() - opened_at < self._cooldown_s:
                log.warning("tool_call circuit_breaker_open tool=%s", request.tool_name)
                return ToolResult(status=ToolStatus.UNAVAILABLE, error="circuit_breaker_open")
            # Cooldown elapsed — allow one attempt through ("half-open").
            del self._opened_at[key]

        result = await call_next(request)
        if result.status in (ToolStatus.FAILED, ToolStatus.TIMEOUT):
            count = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = count
            if count >= self._fails_before_open:
                self._opened_at[key] = time.monotonic()
                log.warning(
                    "tool_call circuit_breaker_opened tool=%s consecutive_failures=%d",
                    request.tool_name, count,
                )
        else:
            self._consecutive_failures[key] = 0
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
) -> MiddlewareChain:
    """Standard chain: Logging, Metrics, Latency (if a store is given), CircuitBreaker, Retry, Timeout."""
    middlewares = [
        LoggingMiddleware(redact_arg_keys=redact_arg_keys),
        MetricsMiddleware(metrics),
    ]
    if latency_store is not None:
        middlewares.append(LatencyRecorderMiddleware(latency_store))
    middlewares += [
        CircuitBreakerMiddleware(),
        RetryMiddleware(),
        TimeoutMiddleware(timeout_ms=timeout_ms),
    ]
    return MiddlewareChain(executor, middlewares)
