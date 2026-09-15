"""
Middleware chain tests — pure unit tests, no network. Uses a fake
IToolExecutor stand-in so each middleware's behavior can be verified in
isolation and in combination.
"""

from __future__ import annotations

import asyncio

from services.conversation.tool_latency import ToolLatencyStore
from services.conversation.tools.executor_registry import ExecutorRegistry
from services.conversation.tools.middleware import (
    CircuitBreakerMiddleware, LatencyRecorderMiddleware, MiddlewareChain, RetryMiddleware,
    TimeoutMiddleware, build_default_chain,
)
from services.conversation.tools.types import ToolExecutionContext, ToolExecutionRequest, ToolResult, ToolStatus


def _ctx(tenant_id: str = "t1", agent_id: str = "a1") -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=tenant_id, agent_id=agent_id, call_id="c1", session_id="s1",
        turn_id="turn1", tool_iteration=0, deadline=0.0, request_id="r1",
    )


def _request(context: ToolExecutionContext | None = None) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_call_id="call1", tool_name="book_appointment", arguments={}, context=context or _ctx(),
    )


class _FixedExecutor:
    def __init__(self, result: ToolResult | None = None, delay_s: float = 0.0, exc: Exception | None = None) -> None:
        self._result = result or ToolResult(status=ToolStatus.SUCCESS, payload={})
        self._delay_s = delay_s
        self._exc = exc
        self.call_count = 0

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        self.call_count += 1
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._exc:
            raise self._exc
        return self._result


async def test_default_chain_passes_through_success():
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    chain = build_default_chain(executor, timeout_ms=1000)

    result = await chain.execute(_request())

    assert result.status == ToolStatus.SUCCESS
    assert result.payload == {"booked": True}
    assert executor.call_count == 1


async def test_timeout_middleware_returns_timeout_status_not_exception():
    executor = _FixedExecutor(delay_s=0.2)
    chain = MiddlewareChain(executor, [TimeoutMiddleware(timeout_ms=50)])

    result = await chain.execute(_request())

    assert result.status == ToolStatus.TIMEOUT


async def test_circuit_breaker_opens_after_consecutive_failures():
    executor = _FixedExecutor(ToolResult(status=ToolStatus.FAILED, error="down"))
    breaker = CircuitBreakerMiddleware(fails_before_open=3, cooldown_s=60.0)
    chain = MiddlewareChain(executor, [breaker])

    results = [await chain.execute(_request()) for _ in range(4)]

    # First 3 calls actually reach the executor and fail; the 4th is
    # short-circuited by the now-open breaker without calling the executor.
    assert [r.status for r in results[:3]] == [ToolStatus.FAILED] * 3
    assert results[3].status == ToolStatus.UNAVAILABLE
    assert executor.call_count == 3


async def test_circuit_breaker_resets_on_success():
    executor = _FixedExecutor(ToolResult(status=ToolStatus.FAILED, error="down"))
    breaker = CircuitBreakerMiddleware(fails_before_open=3, cooldown_s=60.0)
    chain = MiddlewareChain(executor, [breaker])

    await chain.execute(_request())
    await chain.execute(_request())
    executor._result = ToolResult(status=ToolStatus.SUCCESS, payload={})
    await chain.execute(_request())  # resets the consecutive-failure count
    executor._result = ToolResult(status=ToolStatus.FAILED, error="down")
    result = await chain.execute(_request())

    # Only 1 consecutive failure since the reset — breaker still closed.
    assert result.status == ToolStatus.FAILED
    assert executor.call_count == 4


async def test_retry_disabled_by_default_calls_executor_once():
    executor = _FixedExecutor(ToolResult(status=ToolStatus.FAILED, error="down"))
    chain = MiddlewareChain(executor, [RetryMiddleware()])

    result = await chain.execute(_request())

    assert result.status == ToolStatus.FAILED
    assert executor.call_count == 1


async def test_retry_enabled_retries_up_to_max():
    executor = _FixedExecutor(ToolResult(status=ToolStatus.FAILED, error="down"))
    chain = MiddlewareChain(executor, [RetryMiddleware(max_retries=2)])

    result = await chain.execute(_request())

    assert result.status == ToolStatus.FAILED
    assert executor.call_count == 3  # 1 original + 2 retries


async def test_executor_registry_resolves_factory_with_provider():
    registry = ExecutorRegistry()
    registry.register("book_appointment", lambda provider, companion=None: _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS)))

    executor = registry.resolve("book_appointment", provider=object())
    assert executor is not None
    result = await executor.execute(_request())
    assert result.status == ToolStatus.SUCCESS


async def test_executor_registry_unknown_tool_returns_none():
    registry = ExecutorRegistry()
    assert registry.resolve("send_email", provider=object()) is None


async def test_latency_recorder_records_success():
    store = ToolLatencyStore()
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={}))
    chain = build_default_chain(executor, timeout_ms=1000, latency_store=store)

    await chain.execute(_request())

    assert store.average_ms("t1", "a1", "book_appointment") is None  # below _MIN_SAMPLES
    for _ in range(2):
        await chain.execute(_request())
    assert store.average_ms("t1", "a1", "book_appointment") is not None


async def test_latency_recorder_records_timeout():
    store = ToolLatencyStore()
    executor = _FixedExecutor(delay_s=0.2)
    chain = MiddlewareChain(executor, [LatencyRecorderMiddleware(store), TimeoutMiddleware(timeout_ms=50)])

    for _ in range(3):
        result = await chain.execute(_request())
    assert result.status == ToolStatus.TIMEOUT
    assert store.average_ms("t1", "a1", "book_appointment") is not None


async def test_latency_recorder_records_nothing_for_unavailable():
    store = ToolLatencyStore()
    executor = _FixedExecutor(ToolResult(status=ToolStatus.UNAVAILABLE, error="circuit_breaker_open"))
    chain = MiddlewareChain(executor, [LatencyRecorderMiddleware(store)])

    for _ in range(3):
        await chain.execute(_request())

    assert store.average_ms("t1", "a1", "book_appointment") is None


async def test_latency_recorder_records_nothing_for_empty_identity():
    store = ToolLatencyStore()
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={}))
    chain = MiddlewareChain(executor, [LatencyRecorderMiddleware(store)])

    for _ in range(3):
        await chain.execute(_request(_ctx(tenant_id="", agent_id="")))

    assert store.average_ms("", "", "book_appointment") is None


async def test_default_chain_without_store_behaves_identically():
    executor = _FixedExecutor(ToolResult(status=ToolStatus.SUCCESS, payload={"booked": True}))
    chain = build_default_chain(executor, timeout_ms=1000)

    result = await chain.execute(_request())

    assert result.status == ToolStatus.SUCCESS
    assert result.payload == {"booked": True}
    assert executor.call_count == 1
