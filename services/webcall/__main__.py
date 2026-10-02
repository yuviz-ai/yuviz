"""Webcall bridge: browser WebSocket <-> Conversation Service gRPC Converse, for testing an agent without telephony.

Binary frames are PCM16 LE 16 kHz mono both ways; text frames are JSON control messages.
Push-to-talk: the browser sends {"type": "speech_ended"} instead of server-side VAD.
"""

from __future__ import annotations

# Generated stubs import "voiceai.v1" absolutely; must run before importing them.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(
    _os.path.dirname(__file__), "..", "conversation", "generated",
))

import asyncio
import json
import logging
import os
import time
import uuid
import wave
from urllib.parse import parse_qs, urlparse

import grpc
import websockets
from websockets.asyncio.server import ServerConnection, serve

from voiceai.v1 import conversation_pb2 as pb
from voiceai.v1 import conversation_pb2_grpc as pb_grpc

log = logging.getLogger("webcall")

PROTOCOL_VERSION = "1.0"
SAMPLE_RATE = 16000


def _parse_query(path: str) -> dict[str, str]:
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    return {k: v[0] for k, v in qs.items() if v}


def _dump_dir() -> str | None:
    return os.environ.get("WEBCALL_DUMP_AUDIO_DIR")


def _write_wav_dump(session_id: str, utterance_num: int, pcm: bytes) -> None:
    """Debug aid (opt-in via WEBCALL_DUMP_AUDIO_DIR): write an utterance to a WAV file."""
    directory = _dump_dir()
    if not directory or not pcm:
        return
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{session_id}_{utterance_num}.wav")
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm)
    log.info("webcall: wrote audio dump %s (%d bytes)", path, len(pcm))


async def _browser_to_grpc(
    ws: ServerConnection, call, session_id: str, response_watchdog: "ResponseWatchdog",
) -> None:
    """Forward browser audio and control frames onto the gRPC stream."""
    sequence_num = 0
    utterance_num = 0
    current_utterance = bytearray() if _dump_dir() else None
    async for message in ws:
        if isinstance(message, bytes):
            sequence_num += 1
            if current_utterance is not None:
                current_utterance.extend(message)
            await call.write(pb.GatewayMessage(audio_chunk=pb.AudioChunk(
                session_id=session_id,
                sequence_num=sequence_num,
                timestamp_us=int(time.time() * 1_000_000),
                payload=message,
            )))
            continue

        try:
            control = json.loads(message)
        except json.JSONDecodeError:
            log.warning("webcall: malformed control message=%r", message)
            continue

        kind = control.get("type")
        if kind == "speech_ended":
            utterance_num += 1
            if current_utterance is not None:
                _write_wav_dump(session_id, utterance_num, bytes(current_utterance))
                current_utterance.clear()
            response_watchdog.arm()
            await call.write(pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(
                session_id=session_id,
                duration_ms=int(control.get("duration_ms", 0)),
                energy_db=float(control.get("energy_db", 0.0)),
            )))
        elif kind == "cancel":
            await call.write(pb.GatewayMessage(cancel_generation=pb.CancelGeneration(
                session_id=session_id,
            )))
        elif kind == "playback_finished":
            await call.write(pb.GatewayMessage(playback_finished=pb.PlaybackFinished(
                session_id=session_id,
                interrupted=bool(control.get("interrupted", False)),
            )))
        else:
            log.warning("webcall: unknown control type=%r", kind)


class ResponseWatchdog:
    """Tell the browser when no reply arrives after speech_ended (empty STT turns get no response)."""

    def __init__(self, ws: ServerConnection, timeout_s: float = 8.0) -> None:
        self._ws = ws
        self._timeout_s = timeout_s
        self._task: asyncio.Task | None = None

    def arm(self) -> None:
        self.disarm()
        self._task = asyncio.create_task(self._fire())

    def disarm(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _fire(self) -> None:
        await asyncio.sleep(self._timeout_s)
        log.warning("webcall: response watchdog fired after %.0fs — no ServiceMessage arrived", self._timeout_s)
        try:
            await self._ws.send(json.dumps({
                "type": "no_response",
                "message": (
                    f"No response after {self._timeout_s:.0f}s — the agent likely didn't "
                    "recognize any speech in that recording (silence, background noise, or "
                    "audio too quiet/unclear). Try again, speaking clearly and a bit louder."
                ),
            }))
            log.info("webcall: sent no_response message to browser")
        except Exception:
            log.exception("webcall: failed to send no_response message")


async def _grpc_to_browser(ws: ServerConnection, call, response_watchdog: ResponseWatchdog) -> None:
    """Forward ServiceMessages to the browser: TTS audio as binary, the rest as JSON."""
    async for msg in call:
        response_watchdog.disarm()  # anything arriving at all proves the turn isn't stuck
        which = msg.WhichOneof("payload")
        if which == "tts_chunk":
            # Skip empty payloads: Web Audio's createBuffer() throws on 0 frames.
            if msg.tts_chunk.payload:
                await ws.send(msg.tts_chunk.payload)
            if msg.tts_chunk.is_final:
                await ws.send(json.dumps({"type": "tts_chunk_final"}))
        elif which == "service_ready":
            await ws.send(json.dumps({"type": "service_ready"}))
        elif which == "stt_result":
            await ws.send(json.dumps({
                "type": "stt_result", "text": msg.stt_result.text,
                "confidence": msg.stt_result.confidence,
            }))
        elif which == "tts_started":
            await ws.send(json.dumps({"type": "tts_started"}))
        elif which == "assistant_response":
            await ws.send(json.dumps({
                "type": "tts_result", "text": msg.assistant_response.text,
            }))
        elif which == "cancel_ack":
            await ws.send(json.dumps({"type": "cancel_ack"}))
        elif which == "error":
            await ws.send(json.dumps({
                "type": "error", "code": msg.error.code,
                "message": msg.error.message, "fatal": msg.error.fatal,
            }))
            if msg.error.fatal:
                return
        elif which == "end_call":
            await ws.send(json.dumps({
                "type": "end_call", "reason": msg.end_call.reason,
            }))
        # transfer_request/conversation_finalized: not applicable to browser calls.


async def _handle_connection(ws: ServerConnection) -> None:
    params = _parse_query(ws.request.path)
    tenant_slug = params.get("tenant")
    agent_slug = params.get("agent")
    if not tenant_slug or not agent_slug:
        await ws.close(code=1008, reason="missing tenant/agent query params")
        return

    # Default to Envoy so calls load-balance across ConvSvc instances, like the Gateway.
    conv_target = os.environ.get("CONVERSATION_SVC_TARGET", "localhost:10000")
    session_id = str(uuid.uuid4())
    log.info("webcall: session=%s tenant=%s agent=%s -> %s", session_id, tenant_slug, agent_slug, conv_target)

    async with grpc.aio.insecure_channel(conv_target) as channel:
        stub = pb_grpc.ConversationServiceStub(channel)
        call = stub.Converse()

        await call.write(pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version=PROTOCOL_VERSION,
            session_id=session_id,
            tenant_id=tenant_slug,   # semantically a slug — see agent_resolver.py
            script_id=agent_slug,    # semantically a slug — see agent_resolver.py
            codec=pb.AUDIO_CODEC_PCM_S16LE,
            sample_rate=SAMPLE_RATE,
            channels=1,
            direction="test",
        )))

        response_watchdog = ResponseWatchdog(ws)
        try:
            await asyncio.gather(
                _browser_to_grpc(ws, call, session_id, response_watchdog),
                _grpc_to_browser(ws, call, response_watchdog),
            )
        except websockets.exceptions.ConnectionClosed:
            log.info("webcall: browser closed session=%s", session_id)
        except grpc.aio.AioRpcError as exc:
            log.warning("webcall: grpc error session=%s detail=%s", session_id, exc)
        finally:
            response_watchdog.disarm()
            call.cancel()


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    port = int(os.environ.get("PORT", "8300"))
    async with serve(_handle_connection, "0.0.0.0", port):
        log.info("Webcall bridge listening on ws://0.0.0.0:%d", port)
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
