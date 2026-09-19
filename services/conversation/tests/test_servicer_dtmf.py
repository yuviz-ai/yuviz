"""Servicer's `dtmf` case and the out-of-band `asyncio.wait` branch (T12) —
driven through a real gRPC round-trip so a broken session.out_responses/
push_dtmf wiring aborts the stream and fails the assertion, rather than
passing on a mock."""

from __future__ import annotations

import asyncio
import re

import grpc
import grpc.aio
import pytest

from ..session import HandlerResponse
from ..session_finalizer import FinalizationResult, FinalizationStatus
from ..servicer import ConversationServicer
from ..generated.voiceai.v1 import conversation_pb2 as pb
from ..generated.voiceai.v1 import conversation_pb2_grpc as pb_grpc


class _DtmfHandler:
    """Speaks nothing unless prompted, except on a keypress it's told to
    react to — pushes a HandlerResponse onto out_responses, exactly the
    shape CallFlowConversationHandler will use (its driver task is not
    built yet; this fake stands in for it)."""

    def __init__(self) -> None:
        self.out_responses: asyncio.Queue[HandlerResponse] = asyncio.Queue()
        self.digits_seen: list[str] = []

    async def greeting(self, session_id: str) -> list[bytes]:
        return []

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        return HandlerResponse()

    async def on_speech_ended(self, session_id, audio, duration_ms, energy_db):
        return
        yield  # pragma: no cover - make this an async generator

    async def on_cancel(self, session_id: str) -> None:
        pass

    async def on_session_end(self, session_id: str, reason: str, final_state=None) -> None:
        pass

    async def on_dtmf(self, session_id: str, digit: str) -> None:
        self.digits_seen.append(digit)
        await self.out_responses.put(HandlerResponse(tts_payloads=[b"\x00\x01"], end_call=True))

    async def on_transfer_failed(self, session_id, destination, reason):
        return
        yield  # pragma: no cover

    def on_transfer_cancelled(self, session_id: str) -> None:
        pass

    def start_finalization(self, session_id: str) -> None:
        pass

    async def finalize_session(self, session_id: str, reason: str) -> FinalizationResult:
        return FinalizationResult(
            summary="", summary_generated=False, transcript_written=False,
            status=FinalizationStatus.COMPLETED,
        )

    def record_live_stage(self, session_id: str, stage: str) -> None:
        pass


async def _dtmf_handler_factory(ctx) -> _DtmfHandler:
    return _DtmfHandler()


async def _open_dtmf_server() -> tuple[str, grpc.aio.Server, list]:
    handlers: list[_DtmfHandler] = []

    async def factory(ctx):
        h = _DtmfHandler()
        handlers.append(h)
        return h

    server = grpc.aio.server()
    pb_grpc.add_ConversationServiceServicer_to_server(ConversationServicer(factory), server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    return f"localhost:{port}", server, handlers


async def _open_and_handshake(stub, session_id="dtmf-session"):
    stream = stub.Converse()
    await stream.write(pb.GatewayMessage(
        session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id=session_id, tenant_id="test-tenant",
            codec=pb.AUDIO_CODEC_PCM_S16LE, sample_rate=16000, channels=1,
        )
    ))
    ready = await stream.read()
    assert ready.HasField("service_ready")
    return stream


@pytest.mark.asyncio
async def test_dtmf_case_reaches_handler_and_out_of_band_response_streams_out():
    addr, server, handlers = await _open_dtmf_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub)

            await stream.write(pb.GatewayMessage(
                dtmf=pb.DtmfDigit(session_id="dtmf-session", digit="7"),
            ))

            msgs = [await stream.read(), await stream.read(), await stream.read()]
            assert msgs[0].HasField("tts_started")
            assert msgs[1].HasField("tts_chunk") and msgs[1].tts_chunk.payload == b"\x00\x01"
            assert msgs[2].HasField("tts_chunk") and msgs[2].tts_chunk.is_final
            # end_call was requested on the pushed response but this turn had
            # TTS, so it should have been sent as the fourth message.
            end_call_msg = await stream.read()
            assert end_call_msg.HasField("end_call")

            await stream.done_writing()
            assert handlers[0].digits_seen == ["7"]
    finally:
        await server.stop(grace=0)


def test_no_digit_shaped_format_string_in_servicer_source():
    src = open("services/conversation/servicer.py").read()
    assert not re.search(r"digit=%s", src)
