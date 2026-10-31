"""Admission control (issue #186): a full process refuses new calls and reports it."""
import asyncio
from unittest.mock import AsyncMock

import grpc
import pytest

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer, _AT_CAPACITY


def _open(sid: str) -> pb.GatewayMessage:
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        session_id=sid, tenant_id="t1", protocol_version="1.0"))


async def _stream(msg):
    yield msg


async def _failing_factory(ctx):
    raise RuntimeError("stop")


def _servicer(**kw):
    return ConversationServicer(_failing_factory, **kw)


async def _drain(servicer, sid, ctx):
    try:
        async for _ in servicer.Converse(_stream(_open(sid)), ctx):
            pass
    except Exception:
        pass


def _codes(ctx):
    return [c.args[0] for c in ctx.abort.await_args_list]


@pytest.mark.asyncio
async def test_refuses_over_cap_without_touching_live_session():
    s = _servicer(max_sessions=1)
    s._live_sessions.add("live")
    ctx = AsyncMock()
    await _drain(s, "new", ctx)
    ctx.abort.assert_awaited_once_with(grpc.StatusCode.RESOURCE_EXHAUSTED, _AT_CAPACITY)
    assert s._live_sessions == {"live"}


@pytest.mark.asyncio
async def test_under_cap_is_admitted_and_slot_released():
    s = _servicer(max_sessions=2)
    ctx = AsyncMock()
    await _drain(s, "a", ctx)
    assert grpc.StatusCode.RESOURCE_EXHAUSTED not in _codes(ctx)
    assert s._live_sessions == set()


@pytest.mark.asyncio
async def test_capacity_callback_fires_full_then_free():
    events = []
    s = _servicer(max_sessions=1, on_capacity_change=events.append)
    await _drain(s, "a", AsyncMock())
    assert events == [True, False]


@pytest.mark.asyncio
async def test_zero_means_unlimited():
    events = []
    s = _servicer(max_sessions=0, on_capacity_change=events.append)
    s._live_sessions.update(f"s{i}" for i in range(1000))
    ctx = AsyncMock()
    await _drain(s, "x", ctx)
    assert grpc.StatusCode.RESOURCE_EXHAUSTED not in _codes(ctx)
    assert events == []


@pytest.mark.asyncio
async def test_duplicate_id_still_already_exists_at_cap():
    s = _servicer(max_sessions=1)
    s._live_sessions.add("dup")
    ctx = AsyncMock()
    await _drain(s, "dup", ctx)
    assert _codes(ctx) == [grpc.StatusCode.ALREADY_EXISTS]


@pytest.mark.asyncio
async def test_concurrent_burst_never_exceeds_cap():
    peak = 0
    gate = asyncio.Event()
    s = _servicer(max_sessions=5)

    async def factory(ctx):
        nonlocal peak
        peak = max(peak, len(s._live_sessions))
        await gate.wait()
        raise RuntimeError("end")

    s._handler_factory = factory
    ctxs = [AsyncMock() for _ in range(50)]
    tasks = [asyncio.create_task(_drain(s, f"s{i}", ctxs[i])) for i in range(50)]
    await asyncio.sleep(0.2)
    refused = sum(1 for c in ctxs if c.abort.await_count)
    gate.set()
    await asyncio.gather(*tasks)
    assert peak <= 5
    assert refused == 45
    assert s._live_sessions == set()
