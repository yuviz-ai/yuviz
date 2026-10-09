"""Fail-closed routing: unknown/unavailable calls hear a message and are ended."""
from unittest.mock import AsyncMock

import pytest

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.rejection import REJECTION_MESSAGES, RejectionHandler
from services.conversation.servicer import ConversationServicer
from services.conversation.session import RoutingStatus


class FakeTTS:
    def __init__(self, fail: bool = False) -> None:
        self.spoken: list[str] = []
        self._fail = fail

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        return b""

    async def synthesize_stream(self, text: str, sample_rate: int):
        if self._fail:
            raise RuntimeError("tts down")
        self.spoken.append(text)
        yield b"\x01\x02"


async def _stream(*messages):
    for m in messages:
        yield m


def _open(status: int) -> pb.GatewayMessage:
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="", routing_status=status,
    ))


async def _converse(status: int, tts: FakeTTS):
    seen = []

    async def factory(ctx):
        seen.append(ctx.routing_status)
        if ctx.routing_status.rejects_call:
            return RejectionHandler(ctx.routing_status, tts, 16000)
        handler = AsyncMock()
        handler.out_responses = None
        handler.greeting.return_value = [b"\x00"]
        return handler

    out = [m async for m in ConversationServicer(factory).Converse(_stream(_open(status)), AsyncMock())]
    return seen, out


@pytest.mark.parametrize("proto_status, status", [
    (pb.ROUTING_STATUS_UNKNOWN, RoutingStatus.UNKNOWN),
    (pb.ROUTING_STATUS_UNAVAILABLE, RoutingStatus.UNAVAILABLE),
])
async def test_rejected_call_speaks_message_then_ends(proto_status, status):
    tts = FakeTTS()
    seen, out = await _converse(proto_status, tts)

    assert seen == [status]
    assert tts.spoken == [REJECTION_MESSAGES[status]]
    kinds = [m.WhichOneof("payload") for m in out]
    assert kinds[:2] == ["service_ready", "tts_started"]
    assert kinds[-1] == "end_call"
    assert out[-1].end_call.reason == f"routing_{status.value}"
    assert out[-2].tts_chunk.is_final and out[-2].tts_chunk.payload == b""
    assert any(m.tts_chunk.payload == b"\x01\x02" for m in out if m.HasField("tts_chunk"))


async def test_rejected_call_still_ends_when_tts_fails():
    _, out = await _converse(pb.ROUTING_STATUS_UNKNOWN, FakeTTS(fail=True))

    assert out[-1].WhichOneof("payload") == "end_call"
    assert out[-2].tts_chunk.is_final


@pytest.mark.parametrize("proto_status, status", [
    (pb.ROUTING_STATUS_UNSPECIFIED, RoutingStatus.UNSPECIFIED),  # webcall / vobiz clients
    (pb.ROUTING_STATUS_ROUTED, RoutingStatus.ROUTED),
    (pb.ROUTING_STATUS_ROUTED_LKG, RoutingStatus.ROUTED_LKG),
])
async def test_routed_call_is_not_ended(proto_status, status):
    seen, out = await _converse(proto_status, FakeTTS())

    assert seen == [status]
    assert "end_call" not in [m.WhichOneof("payload") for m in out]


async def test_caller_talking_over_rejection_hears_it_again_and_call_ends():
    tts = FakeTTS()
    handler = RejectionHandler(RoutingStatus.UNKNOWN, tts, 16000)

    responses = [r async for r in handler.on_speech_ended("s1", b"", 500, -20.0)]

    assert len(responses) == 1
    assert responses[0].end_call and responses[0].stt_text
    assert tts.spoken == [REJECTION_MESSAGES[RoutingStatus.UNKNOWN]]
