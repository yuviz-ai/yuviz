"""
Integration tests: ConversationServicer + EchoConversationHandler over an
in-process loopback gRPC server (no C++ gateway).
"""

from __future__ import annotations

import asyncio
import struct

import grpc
import grpc.aio
import pytest

from ..echo import EchoConversationHandler
from ..servicer import ConversationServicer
from ..generated.voiceai.v1 import conversation_pb2 as pb
from ..generated.voiceai.v1 import conversation_pb2_grpc as pb_grpc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pcm_sine(n_samples: int = 320, amplitude: int = 10000) -> bytes:
    """Generate n_samples of 16-bit signed little-endian sine-ish samples."""
    import math
    samples = [
        int(amplitude * math.sin(2 * math.pi * i / 32))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


async def _echo_handler_factory(ctx) -> EchoConversationHandler:
    return EchoConversationHandler()


async def _open_echo_server() -> tuple[str, grpc.aio.Server]:
    server = grpc.aio.server()
    pb_grpc.add_ConversationServiceServicer_to_server(
        ConversationServicer(_echo_handler_factory),
        server,
    )
    port = server.add_insecure_port("[::]:0")
    await server.start()
    return f"localhost:{port}", server


async def _open_and_handshake(
    stub: pb_grpc.ConversationServiceStub,
    session_id: str = "test-session",
) -> "grpc.aio.StreamStreamCall":
    """Open a Converse stream and consume the ServiceReady message."""
    stream = stub.Converse()
    await stream.write(pb.GatewayMessage(
        session_open=pb.SessionOpenRequest(
            protocol_version="1.0",
            session_id=session_id,
            tenant_id="test-tenant",
            codec=pb.AUDIO_CODEC_PCM_S16LE,
            sample_rate=16000,
            channels=1,
        )
    ))
    ready = await stream.read()
    assert ready.HasField("service_ready"), f"Expected service_ready, got: {ready}"
    assert ready.service_ready.session_id == session_id
    return stream


# ---------------------------------------------------------------------------
# Handshake tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_ready_on_open():
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub = pb_grpc.ConversationServiceStub(channel)
            stream = stub.Converse()
            await stream.write(pb.GatewayMessage(
                session_open=pb.SessionOpenRequest(
                    protocol_version="1.0",
                    session_id="test-session-001",
                    tenant_id="test-tenant",
                    codec=pb.AUDIO_CODEC_PCM_S16LE,
                    sample_rate=16000,
                    channels=1,
                )
            ))
            ready = await stream.read()
            assert ready.HasField("service_ready")
            assert ready.service_ready.session_id == "test-session-001"
            assert ready.service_ready.protocol_version == "1.0"
            await stream.done_writing()
    finally:
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_rejects_unknown_protocol_version():
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = stub.Converse()
            await stream.write(pb.GatewayMessage(
                session_open=pb.SessionOpenRequest(
                    protocol_version="99.0",
                    session_id="test-bad-version",
                )
            ))
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stream.read()
            assert exc_info.value.code() == grpc.StatusCode.UNIMPLEMENTED
    finally:
        await server.stop(grace=0)


# ---------------------------------------------------------------------------
# Phase 4 message sequence: stt_result → tts_started → tts_chunk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_chunk_yields_full_phase4_sequence():
    """audio_chunk yields stt_result, tts_started, tts_chunk in order (drives the gateway FSM)."""
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-phase4-seq")

            payload = _pcm_sine(320)
            await stream.write(pb.GatewayMessage(
                audio_chunk=pb.AudioChunk(
                    session_id="test-phase4-seq",
                    sequence_num=1,
                    payload=payload,
                )
            ))

            # 1. stt_result
            stt = await stream.read()
            assert stt.HasField("stt_result"), f"Expected stt_result, got: {stt}"
            assert stt.stt_result.text == "echo"
            assert stt.stt_result.confidence == pytest.approx(1.0)
            assert stt.stt_result.is_final is True
            assert stt.stt_result.session_id == "test-phase4-seq"

            # 2. tts_started
            tts_s = await stream.read()
            assert tts_s.HasField("tts_started"), f"Expected tts_started, got: {tts_s}"
            assert tts_s.tts_started.session_id == "test-phase4-seq"

            # 3. tts_chunk (echo)
            chunk = await stream.read()
            assert chunk.HasField("tts_chunk"), f"Expected tts_chunk, got: {chunk}"
            assert chunk.tts_chunk.payload == payload
            assert chunk.tts_chunk.is_final is True
            assert chunk.tts_chunk.sample_rate == 16000

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_multiple_audio_chunks_each_get_full_sequence():
    """Each audio_chunk independently produces stt_result + tts_started + tts_chunk."""
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-multi-seq")

            payloads = [_pcm_sine(320, amp) for amp in (5000, 8000, 12000)]
            for i, p in enumerate(payloads):
                await stream.write(pb.GatewayMessage(
                    audio_chunk=pb.AudioChunk(
                        session_id="test-multi-seq",
                        sequence_num=i + 1,
                        payload=p,
                    )
                ))
                stt   = await stream.read()
                tts_s = await stream.read()
                echo  = await stream.read()
                assert stt.HasField("stt_result")
                assert tts_s.HasField("tts_started")
                assert echo.HasField("tts_chunk")
                assert echo.tts_chunk.payload == p
                assert echo.tts_chunk.sequence_num == i + 1

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


# ---------------------------------------------------------------------------
# Cancel / barge-in
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_generation_sends_ack():
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-cancel")

            await stream.write(pb.GatewayMessage(
                cancel_generation=pb.CancelGeneration(
                    session_id="test-cancel",
                    trace_id="trace-abc",
                )
            ))
            ack = await stream.read()
            assert ack.HasField("cancel_ack")
            assert ack.cancel_ack.session_id == "test-cancel"

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


# ---------------------------------------------------------------------------
# Playback finished + multi-turn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_playback_finished_accepted_without_error():
    """PlaybackFinished is accepted and the stream stays usable."""
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-playback-fin")

            payload = _pcm_sine(320)
            await stream.write(pb.GatewayMessage(
                audio_chunk=pb.AudioChunk(session_id="test-playback-fin",
                                          sequence_num=1, payload=payload)
            ))
            # Drain stt_result + tts_started + tts_chunk
            await stream.read()
            await stream.read()
            await stream.read()

            # Send PlaybackFinished — servicer must not error.
            await stream.write(pb.GatewayMessage(
                playback_finished=pb.PlaybackFinished(
                    session_id="test-playback-fin",
                    interrupted=False,
                )
            ))

            # Stream is still alive — can send another chunk.
            await stream.write(pb.GatewayMessage(
                audio_chunk=pb.AudioChunk(session_id="test-playback-fin",
                                          sequence_num=2, payload=payload)
            ))
            stt2 = await stream.read()
            assert stt2.HasField("stt_result"), "Second turn must also produce stt_result"

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_multi_turn_conversation():
    """Two full turns separated by PlaybackFinished both complete."""
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-multi-turn")

            for turn in range(1, 3):
                p = _pcm_sine(320, turn * 4000)

                await stream.write(pb.GatewayMessage(
                    audio_chunk=pb.AudioChunk(
                        session_id="test-multi-turn",
                        sequence_num=turn,
                        payload=p,
                    )
                ))

                stt   = await stream.read()
                tts_s = await stream.read()
                chunk = await stream.read()

                assert stt.HasField("stt_result"),   f"turn {turn}: missing stt_result"
                assert tts_s.HasField("tts_started"), f"turn {turn}: missing tts_started"
                assert chunk.HasField("tts_chunk"),   f"turn {turn}: missing tts_chunk"
                assert chunk.tts_chunk.payload == p,  f"turn {turn}: wrong payload"

                await stream.write(pb.GatewayMessage(
                    playback_finished=pb.PlaybackFinished(
                        session_id="test-multi-turn",
                        interrupted=False,
                    )
                ))

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


# ---------------------------------------------------------------------------
# EventBus regression (Phase 3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bus_drain_loop_fires_subscribers():
    """Subscribers receive events only after EventBus.start(), including ones queued before."""
    from ..event_bus import EventBus, SessionEnded

    received: list = []

    async def on_ended(event: SessionEnded) -> None:
        received.append(event)

    bus = EventBus()
    bus.subscribe(SessionEnded, on_ended)

    # Without start(), publish() enqueues but nothing drains.
    bus.publish(SessionEnded(session_id="s1", reason="test"))
    await asyncio.sleep(0)
    assert received == [], "sanity: drain loop must not be running before start()"

    await bus.start()
    bus.publish(SessionEnded(session_id="s2", reason="test"))
    await asyncio.sleep(0.05)
    # Both s1 (queued before start) and s2 are delivered.
    assert len(received) == 2
    assert received[0].session_id == "s1"
    assert received[1].session_id == "s2"

    await bus.stop()


# ---------------------------------------------------------------------------
# Transfer notifications over the real wire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_initiated_and_completed_round_trip_without_breaking_stream():
    """transfer_initiated + transfer_completed over the wire leave the stream closing cleanly."""
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-transfer-notify")

            await stream.write(pb.GatewayMessage(
                transfer_initiated=pb.TransferInitiated(
                    session_id="test-transfer-notify",
                    transfer_type="cold",
                    destination="+15551234567",
                    reason="escalation_threshold_exceeded",
                )
            ))
            await stream.write(pb.GatewayMessage(
                transfer_completed=pb.TransferCompleted(
                    session_id="test-transfer-notify",
                    destination="+15551234567",
                )
            ))

            await stream.done_writing()
            # Drain via read(): mixing write() with async iteration on one stream isn't allowed.
            while await stream.read() is not grpc.aio.EOF:
                pass
    finally:
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_transfer_failed_round_trip_without_breaking_stream():
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-transfer-fail-notify")

            await stream.write(pb.GatewayMessage(
                transfer_initiated=pb.TransferInitiated(
                    session_id="test-transfer-fail-notify",
                    transfer_type="cold",
                    destination="+15551234567",
                    reason="x",
                )
            ))
            await stream.write(pb.GatewayMessage(
                transfer_failed=pb.TransferFailed(
                    session_id="test-transfer-fail-notify",
                    destination="+15551234567",
                    reason="hangup_before_bridge",
                )
            ))

            await stream.done_writing()
            while await stream.read() is not grpc.aio.EOF:
                pass
    finally:
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_transfer_completed_sends_conversation_finalized_over_the_wire():
    """transfer_completed yields a ConversationFinalized ServiceMessage (the gateway waits on it)."""
    addr, server = await _open_echo_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-finalize-notify")

            await stream.write(pb.GatewayMessage(
                transfer_initiated=pb.TransferInitiated(
                    session_id="test-finalize-notify",
                    transfer_type="cold",
                    destination="+15551234567",
                    reason="x",
                )
            ))
            await stream.write(pb.GatewayMessage(
                transfer_completed=pb.TransferCompleted(
                    session_id="test-finalize-notify",
                    destination="+15551234567",
                )
            ))

            finalized = await stream.read()
            assert finalized.HasField("conversation_finalized")
            cf = finalized.conversation_finalized
            assert cf.session_id == "test-finalize-notify"
            assert cf.reason == pb.TRANSFER_SUCCESS
            # Echo handler has no LLM/transcripts.
            assert cf.summary_generated is False
            assert cf.transcript_written is False

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


# ---------------------------------------------------------------------------
# TransferRequest over the wire
# ---------------------------------------------------------------------------

from ..directives import TransferRequest, TransferType
from ..session import HandlerResponse


class _TransferringEchoHandler(EchoConversationHandler):
    """speech_ended turn requests a cold transfer with a spoken acknowledgment."""

    async def on_speech_ended(self, session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(
            stt_text="I want a human",
            stt_confidence=1.0,
            tts_payloads=[_pcm_sine(320)],
            transfer_request=TransferRequest(
                session_id=session_id,
                tenant_id="test-tenant",
                call_id=session_id,
                transfer_type=TransferType.COLD,
                destination="1001",
                reason="caller_requested_human",
            ),
        )


async def _open_transfer_server() -> tuple[str, grpc.aio.Server]:
    async def factory(ctx):
        return _TransferringEchoHandler()
    server = grpc.aio.server()
    pb_grpc.add_ConversationServiceServicer_to_server(
        ConversationServicer(factory), server,
    )
    port = server.add_insecure_port("[::]:0")
    await server.start()
    return f"localhost:{port}", server


async def _drive_transfer_turn(stream, session_id: str) -> None:
    """speech_ended → stt_result → tts_started → tts_chunk(s incl. final
    empty marker). Leaves the stream right before playback_finished."""
    await stream.write(pb.GatewayMessage(
        speech_ended=pb.SpeechEndedNotification(
            session_id=session_id, duration_ms=500, energy_db=-20.0,
        )
    ))
    stt = await stream.read()
    assert stt.HasField("stt_result")
    tts_s = await stream.read()
    assert tts_s.HasField("tts_started")
    chunk = await stream.read()
    assert chunk.HasField("tts_chunk")
    while not chunk.tts_chunk.is_final:
        chunk = await stream.read()
        assert chunk.HasField("tts_chunk")


@pytest.mark.asyncio
async def test_transfer_request_sent_after_uninterrupted_playback():
    """The TransferRequest gRPC message must reach the gateway — held until
    the acknowledgment turn's audio finishes playing, then sent."""
    addr, server = await _open_transfer_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-transfer-send")
            await _drive_transfer_turn(stream, "test-transfer-send")

            # No TransferRequest yet — it is held for playback completion.
            await stream.write(pb.GatewayMessage(
                playback_finished=pb.PlaybackFinished(
                    session_id="test-transfer-send", interrupted=False,
                )
            ))
            msg = await asyncio.wait_for(stream.read(), timeout=5)
            assert msg.HasField("transfer_request"), f"Expected transfer_request, got: {msg}"
            assert msg.transfer_request.transfer_type == "cold"
            assert msg.transfer_request.destination == "1001"
            assert msg.transfer_request.reason == "caller_requested_human"
            assert msg.transfer_request.session_id == "test-transfer-send"
            assert msg.transfer_request.transfer_id

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_transfer_request_dropped_on_interrupted_playback():
    """Barge-in during the acknowledgment cancels the pending transfer —
    the LLM re-emits the directive if the caller still wants a human."""
    addr, server = await _open_transfer_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-transfer-drop")
            await _drive_transfer_turn(stream, "test-transfer-drop")

            await stream.write(pb.GatewayMessage(
                playback_finished=pb.PlaybackFinished(
                    session_id="test-transfer-drop", interrupted=True,
                )
            ))
            # Nothing must arrive: the pending transfer was dropped.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(stream.read(), timeout=1)

            await stream.done_writing()
    finally:
        await server.stop(grace=0)


# ---------------------------------------------------------------------------
# assistant_response (turn text for the browser test-call panel)
# ---------------------------------------------------------------------------

class _SpeakingEchoHandler(EchoConversationHandler):
    """speech_ended turn carries both audio and the turn's full text."""

    async def on_speech_ended(self, session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(
            stt_text="what's the weather",
            stt_confidence=1.0,
            tts_payloads=[_pcm_sine(320)],
        )
        yield HandlerResponse(response_text="It's sunny today.")


async def _open_speaking_server() -> tuple[str, grpc.aio.Server]:
    async def factory(ctx):
        return _SpeakingEchoHandler()
    server = grpc.aio.server()
    pb_grpc.add_ConversationServiceServicer_to_server(
        ConversationServicer(factory), server,
    )
    port = server.add_insecure_port("[::]:0")
    await server.start()
    return f"localhost:{port}", server


@pytest.mark.asyncio
async def test_assistant_response_sent_after_turn_audio():
    """response_text arrives as assistant_response after the turn's audio."""
    addr, server = await _open_speaking_server()
    try:
        async with grpc.aio.insecure_channel(addr) as channel:
            stub   = pb_grpc.ConversationServiceStub(channel)
            stream = await _open_and_handshake(stub, "test-assistant-response")

            await stream.write(pb.GatewayMessage(
                speech_ended=pb.SpeechEndedNotification(
                    session_id="test-assistant-response", duration_ms=500, energy_db=-20.0,
                )
            ))
            stt = await stream.read()
            assert stt.HasField("stt_result")
            tts_s = await stream.read()
            assert tts_s.HasField("tts_started")
            chunk = await stream.read()
            assert chunk.HasField("tts_chunk")

            msg = await asyncio.wait_for(stream.read(), timeout=5)
            assert msg.HasField("assistant_response"), f"Expected assistant_response, got: {msg}"
            assert msg.assistant_response.text == "It's sunny today."
            assert msg.assistant_response.session_id == "test-assistant-response"

            await stream.done_writing()
    finally:
        await server.stop(grace=0)
