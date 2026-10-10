"""Fail-closed routing: unknown/unavailable calls hear a message and are ended."""
from unittest.mock import AsyncMock

import pytest

from services.conversation import rejection

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


@pytest.fixture(autouse=True)
def _fresh_rejection_audio():
    rejection._audio_cache.clear()
    yield
    rejection._audio_cache.clear()


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


async def test_tts_failure_still_sends_audio_so_end_call_is_sent():
    handler = RejectionHandler(RoutingStatus.UNKNOWN, FakeTTS(fail=True), 16000)

    greeting = await handler.greeting("s1")
    [repeat] = [r async for r in handler.on_speech_ended("s1", b"", 500, -20.0)]

    assert greeting and all(greeting)
    assert repeat.end_call and repeat.tts_payloads and all(repeat.tts_payloads)


async def test_failed_synthesis_is_not_cached():
    await RejectionHandler(RoutingStatus.UNKNOWN, FakeTTS(fail=True), 16000).greeting("s1")
    tts = FakeTTS()

    await RejectionHandler(RoutingStatus.UNKNOWN, tts, 16000).greeting("s2")

    assert tts.spoken == [REJECTION_MESSAGES[RoutingStatus.UNKNOWN]]


async def test_message_is_synthesized_once_across_calls():
    tts = FakeTTS()
    await rejection.prewarm(tts, 16000)

    for sid in ("s1", "s2", "s3"):
        await RejectionHandler(RoutingStatus.UNKNOWN, tts, 16000).greeting(sid)

    assert sorted(tts.spoken) == sorted(REJECTION_MESSAGES.values())


async def test_repeated_talk_over_hangs_up_without_speaking_again():
    tts = FakeTTS()
    handler = RejectionHandler(RoutingStatus.UNKNOWN, tts, 16000)

    [first] = [r async for r in handler.on_speech_ended("s1", b"", 500, -20.0)]
    with pytest.raises(rejection.RejectedCallHangup):
        [r async for r in handler.on_speech_ended("s1", b"", 500, -20.0)]

    assert first.end_call
    assert tts.spoken == [REJECTION_MESSAGES[RoutingStatus.UNKNOWN]]


async def test_servicer_turns_repeat_cap_into_fatal_error():
    async def factory(ctx):
        handler = AsyncMock()
        handler.out_responses = None
        handler.greeting.return_value = [b"\x00"]

        async def on_speech_ended(*_):
            raise rejection.RejectedCallHangup("cap")
            yield
        handler.on_speech_ended = on_speech_ended
        return handler

    speech = pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=500))
    out = [m async for m in ConversationServicer(factory).Converse(
        _stream(_open(pb.ROUTING_STATUS_UNKNOWN), speech), AsyncMock())]

    [err] = [m.error for m in out if m.WhichOneof("payload") == "error"]
    assert err.fatal and err.code == "CALL_REJECTED"


async def test_unknown_routing_value_is_rejected_not_routed():
    seen, out = await _converse(99, FakeTTS())

    assert seen == [RoutingStatus.UNAVAILABLE]
    assert [m.WhichOneof("payload") for m in out][-1] == "end_call"


async def test_routed_call_rejected_by_the_factory_still_hangs_up_after_the_message():
    # The handler factory rejects a routed call whose agent config can't be loaded.
    async def factory(ctx):
        ctx.routing_status = RoutingStatus.UNAVAILABLE
        return RejectionHandler(RoutingStatus.UNAVAILABLE, FakeTTS(), 16000)

    out = [m async for m in ConversationServicer(factory).Converse(
        _stream(_open(pb.ROUTING_STATUS_ROUTED)), AsyncMock())]

    kinds = [m.WhichOneof("payload") for m in out]
    assert kinds[-1] == "end_call"
    assert out[-1].end_call.reason == "routing_unavailable"
