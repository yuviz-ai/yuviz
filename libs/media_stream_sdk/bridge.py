"""Provider-agnostic Media Stream <-> Conversation Service bridge with local SileroVAD.

Playback is paced at real time so barge-in has unsent audio to clear. Caller audio
is held _AUDIO_DELAY_S so CancelGeneration reaches the server before the barge-in words.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import uuid
import wave
from typing import Callable

import grpc
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from voiceai.v1 import conversation_pb2 as pb
from voiceai.v1 import conversation_pb2_grpc as pb_grpc

from libs.vad_sdk.silero_vad import SileroVAD, WINDOW_BYTES as _VAD_FRAME_BYTES
from libs.vad_sdk.vad import VADEvent

from .audio import AudioBridge
from .serializers import MediaStreamSerializer

PROTOCOL_VERSION = "1.0"
PIPELINE_SAMPLE_RATE = 16000
_ULAW_FRAME_BYTES = 160  # 20ms @ 8kHz, 1 byte/sample mu-law
_PLAYBACK_LEAD_S = 0.1   # matches the Gateway PlaybackDrain's kLead

# Covers SileroVAD's 96ms onset with margin, so pre-roll audio can follow CancelGeneration.
_AUDIO_DELAY_S = 0.15

# Queued after a turn's final TTS byte: "everything before this is drained".
_FINAL_MARKER = object()

# Silent turns send no is_final; without this, _turn_active sticks and every
# later utterance is misread as barge-in.
_TURN_WATCHDOG_S = 12.0


class MediaStreamBridge:
    """One per live call; provider wire shapes live only behind `serializer`."""

    def __init__(self, *, serializer: MediaStreamSerializer, call_id: str,
                 tenant_slug: str, agent_slug: str, direction: str,
                 caller_did: str = "", called_did: str = "",
                 log_name: str = "media_stream",
                 on_session_start: Callable[[str], None] | None = None) -> None:
        self._serializer = serializer
        self.call_id = call_id
        self.tenant_slug = tenant_slug
        self.agent_slug = agent_slug
        self.direction = direction
        self.caller_did = caller_did
        self.called_did = called_did
        self.log = logging.getLogger(log_name)
        # Called once with the session_id generated in run(), so a caller
        # (e.g. Vobiz) can learn it while the call is still in progress.
        self._on_session_start = on_session_start

        self._audio = AudioBridge()
        self._vad = SileroVAD()
        self._vad_buf = bytearray()
        self._sequence_num = 0
        self._stream_id: str | None = None
        self._speaking = False     # True while we're inside a detected caller utterance
        self._playing_tts = False  # True from first paced frame sent until the turn fully drains
        self._turn_active = False  # True from speech_ended sent until that turn resolves
        self._session_id = ""
        self._turn_generation = 0
        self._turn_watchdog: asyncio.Task | None = None

        self._play_queue: asyncio.Queue = asyncio.Queue()
        self._play_buf = bytearray()       # leftover sub-frame bytes between gRPC chunks
        self._playback_started_at: float | None = None
        self._frames_sent = 0

        # Single writer for the gRPC stream so send order is exactly enqueue order.
        self._grpc_write_queue: asyncio.Queue = asyncio.Queue()
        # (enqueued_at_monotonic, AudioChunk proto) pending their _AUDIO_DELAY_S hold.
        self._audio_delay_buf: collections.deque = collections.deque()

        self._dump_dir = os.environ.get("MEDIA_STREAM_DUMP_DIR")
        self._inbound_wav: wave.Wave_write | None = None
        self._outbound_wav: wave.Wave_write | None = None

    def _open_dumps(self) -> None:
        """Debug aid (MEDIA_STREAM_DUMP_DIR): dump the whole call's PCM16 to WAV."""
        if not self._dump_dir:
            return
        os.makedirs(self._dump_dir, exist_ok=True)
        self._inbound_wav = wave.open(
            os.path.join(self._dump_dir, f"{self.call_id}-inbound.wav"), "wb")
        self._inbound_wav.setnchannels(1)
        self._inbound_wav.setsampwidth(2)
        self._inbound_wav.setframerate(PIPELINE_SAMPLE_RATE)
        self._outbound_wav = wave.open(
            os.path.join(self._dump_dir, f"{self.call_id}-outbound.wav"), "wb")
        self._outbound_wav.setnchannels(1)
        self._outbound_wav.setsampwidth(2)
        self._outbound_wav.setframerate(PIPELINE_SAMPLE_RATE)

    def _close_dumps(self) -> None:
        if self._inbound_wav is not None:
            self._inbound_wav.close()
            self._inbound_wav = None
        if self._outbound_wav is not None:
            self._outbound_wav.close()
            self._outbound_wav = None
        if self._dump_dir:
            self.log.info("wrote audio dumps for call=%s to %s", self.call_id, self._dump_dir)

    def _clear_playback_queue(self) -> None:
        """Drop every not-yet-sent frame (barge-in)."""
        while True:
            try:
                self._play_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._play_buf.clear()
        self._playback_started_at = None
        self._frames_sent = 0

    def _send_playback_finished(self, interrupted: bool) -> None:
        # Same signal the Gateway sends; the servicer releases a held
        # TransferRequest (and advances its FSM) only on this.
        self._grpc_write_queue.put_nowait(pb.GatewayMessage(playback_finished=pb.PlaybackFinished(
            session_id=self._session_id, interrupted=interrupted,
        )))

    def _flush_pending_audio_now(self) -> None:
        """Enqueue all held pre-roll audio now, bypassing the delay."""
        while self._audio_delay_buf:
            _, msg = self._audio_delay_buf.popleft()
            self._grpc_write_queue.put_nowait(msg)

    async def _grpc_writer_loop(self, call) -> None:
        """The only caller of call.write()."""
        while True:
            msg = await self._grpc_write_queue.get()
            await call.write(msg)

    async def _audio_delay_pump(self) -> None:
        """Release each audio_chunk to the writer after _AUDIO_DELAY_S."""
        while True:
            if not self._audio_delay_buf:
                await asyncio.sleep(0.01)
                continue
            enqueued_at, msg = self._audio_delay_buf[0]
            wait = _AUDIO_DELAY_S - (time.monotonic() - enqueued_at)
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            self._audio_delay_buf.popleft()
            self._grpc_write_queue.put_nowait(msg)

    def _resolve_turn(self) -> None:
        self._turn_active = False
        if self._turn_watchdog is not None:
            self._turn_watchdog.cancel()
            self._turn_watchdog = None

    def _start_turn(self) -> None:
        self._turn_active = True
        self._turn_generation += 1
        gen = self._turn_generation
        if self._turn_watchdog is not None:
            self._turn_watchdog.cancel()
        self._turn_watchdog = asyncio.create_task(self._turn_watchdog_fire(gen))

    async def _turn_watchdog_fire(self, gen: int) -> None:
        await asyncio.sleep(_TURN_WATCHDOG_S)
        if self._turn_active and gen == self._turn_generation:
            self.log.info(
                "turn watchdog fired call=%s — no is_final within %.0fs, "
                "assuming silent/dropped turn resolved",
                self.call_id, _TURN_WATCHDOG_S,
            )
            self._turn_active = False
            self._turn_watchdog = None

    async def run(self, ws) -> None:
        # Envoy's gRPC proxy, so calls load-balance across ConvSvc instances.
        conv_target = os.environ.get("CONVERSATION_SVC_TARGET", "localhost:10000")
        session_id = str(uuid.uuid4())
        self._session_id = session_id
        self.log.info(
            "session=%s call=%s tenant=%s agent=%s dir=%s -> %s",
            session_id, self.call_id, self.tenant_slug, self.agent_slug, self.direction, conv_target,
        )
        if self._on_session_start is not None:
            self._on_session_start(session_id)
        self._open_dumps()

        async with grpc.aio.insecure_channel(conv_target) as channel:
            stub = pb_grpc.ConversationServiceStub(channel)
            call = stub.Converse()

            await call.write(pb.GatewayMessage(session_open=pb.SessionOpenRequest(
                protocol_version=PROTOCOL_VERSION,
                session_id=session_id,
                tenant_id=self.tenant_slug,
                script_id=self.agent_slug,
                call_id=self.call_id,
                caller_did=self.caller_did,
                called_did=self.called_did,
                codec=pb.AUDIO_CODEC_PCM_S16LE,
                sample_rate=PIPELINE_SAMPLE_RATE,
                channels=1,
                direction=self.direction,
            )))

            tasks = [
                asyncio.create_task(self._vobiz_to_grpc(ws, session_id)),
                asyncio.create_task(self._grpc_to_vobiz(ws, call)),
                asyncio.create_task(self._playback_pacer(ws)),
                asyncio.create_task(self._grpc_writer_loop(call)),
                asyncio.create_task(self._audio_delay_pump()),
            ]
            try:
                # Only the WS reader and gRPC reader ever finish; either ends the call.
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
            except (WebSocketDisconnect, ConnectionClosed):
                # A trailing TTS chunk racing the caller's hangup is expected.
                self.log.info("caller disconnected mid-stream call=%s", self.call_id)
            except grpc.aio.AioRpcError as exc:
                # A hangup mid-read surfaces as CANCELLED/UNAVAILABLE/RST_STREAM here.
                if exc.code() in (
                    grpc.StatusCode.CANCELLED, grpc.StatusCode.UNAVAILABLE,
                ) or "RST_STREAM" in (exc.details() or ""):
                    self.log.info("caller disconnected mid-stream call=%s", self.call_id)
                else:
                    self.log.exception("bridge error call=%s", self.call_id)
            except Exception:
                self.log.exception("bridge error call=%s", self.call_id)
            finally:
                for task in tasks:
                    task.cancel()
                call.cancel()
                if self._turn_watchdog is not None:
                    self._turn_watchdog.cancel()
                self._close_dumps()

    async def _vobiz_to_grpc(self, ws, session_id: str) -> None:
        async for raw in ws.iter_text():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                self.log.warning("malformed frame call=%s", self.call_id)
                continue

            kind = self._serializer.event_kind(event)
            if kind == "start":
                self._stream_id = self._serializer.stream_id(event)
            elif kind == "media":
                payload_b64 = self._serializer.media_payload(event)
                if not payload_b64:
                    continue
                pcm16 = self._audio.to_pcm16(payload_b64)
                if self._inbound_wav is not None:
                    self._inbound_wav.writeframes(pcm16)
                self._sequence_num += 1
                audio_msg = pb.GatewayMessage(audio_chunk=pb.AudioChunk(
                    session_id=session_id,
                    sequence_num=self._sequence_num,
                    timestamp_us=int(time.time() * 1_000_000),
                    payload=pcm16,
                ))
                # Held for _AUDIO_DELAY_S; flushed early on barge-in.
                self._audio_delay_buf.append((time.monotonic(), audio_msg))
                await self._run_vad(ws, session_id, pcm16)
            elif kind == "dtmf":
                digit = self._serializer.dtmf_digit(event)
                if not digit:
                    continue
                self.log.info("dtmf received call=%s", self.call_id)
                # Flush held audio so the digit is ordered after audio from the same instant.
                self._flush_pending_audio_now()
                self._grpc_write_queue.put_nowait(pb.GatewayMessage(
                    dtmf=pb.DtmfDigit(session_id=session_id, digit=digit),
                ))
            elif kind == "stop":
                self.log.info("stream stop call=%s", self.call_id)
                return

    async def _run_vad(self, ws, session_id: str, pcm16: bytes) -> None:
        self._vad_buf.extend(pcm16)
        while len(self._vad_buf) >= _VAD_FRAME_BYTES:
            frame = bytes(self._vad_buf[:_VAD_FRAME_BYTES])
            del self._vad_buf[:_VAD_FRAME_BYTES]

            vad_event = self._vad.process(frame)
            if vad_event == VADEvent.SPEECH_START:
                self._speaking = True
                if self._turn_active:
                    was_playing = self._playing_tts
                    self._resolve_turn()
                    self._clear_playback_queue()
                    self._playing_tts = False
                    self.log.info(
                        "barge-in detected call=%s (was_playing=%s, speech_prob=%.3f)",
                        self.call_id, was_playing, self._vad.last_speech_prob,
                    )
                    if was_playing:
                        clear_frame = self._serializer.clear_playback(self._stream_id)
                        if clear_frame is not None:
                            await ws.send_text(clear_frame)
                    # Cancel before the held pre-roll, or the server discards the
                    # caller's first words as leftovers of the cancelled turn.
                    self._grpc_write_queue.put_nowait(pb.GatewayMessage(cancel_generation=pb.CancelGeneration(
                        session_id=session_id,
                    )))
                    self._send_playback_finished(interrupted=True)
                    self._flush_pending_audio_now()
            elif vad_event == VADEvent.SPEECH_END and self._speaking:
                self._speaking = False
                self._start_turn()
                # energy_db is observational only; Silero has no dB, so send speech prob.
                self._grpc_write_queue.put_nowait(pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(
                    session_id=session_id,
                    duration_ms=self._vad.speech_duration_ms,
                    energy_db=self._vad.last_speech_prob,
                )))

    async def _grpc_to_vobiz(self, ws, call) -> None:
        """Feed TTS into the pacer queue; unpaced audio would defeat barge-in."""
        async for msg in call:
            which = msg.WhichOneof("payload")
            if which == "tts_chunk":
                if msg.tts_chunk.payload:
                    if self._outbound_wav is not None:
                        self._outbound_wav.writeframes(msg.tts_chunk.payload)
                    ulaw = self._audio.from_pcm16(msg.tts_chunk.payload)
                    self._play_buf.extend(ulaw)
                    while len(self._play_buf) >= _ULAW_FRAME_BYTES:
                        frame = bytes(self._play_buf[:_ULAW_FRAME_BYTES])
                        del self._play_buf[:_ULAW_FRAME_BYTES]
                        await self._play_queue.put(frame)
                if msg.tts_chunk.is_final:
                    if self._play_buf:
                        await self._play_queue.put(bytes(self._play_buf))
                        self._play_buf.clear()
                    await self._play_queue.put(_FINAL_MARKER)
            elif which == "cancel_ack":
                # Barge-in was already handled in _run_vad.
                self.log.debug("cancel_ack call=%s", self.call_id)
            elif which == "error":
                self.log.warning("pipeline error call=%s code=%s message=%s",
                                  self.call_id, msg.error.code, msg.error.message)
                self._clear_playback_queue()
                self._playing_tts = False
                self._resolve_turn()
                if msg.error.fatal:
                    return
            elif which == "transfer_request":
                # No provider call-control path for transfers yet: fail at
                # once so the caller hears the failed-transfer fallback
                # instead of silence until the pipeline's transfer timeout.
                tr = msg.transfer_request
                self.log.warning("transfer_request unsupported on provider calls call=%s "
                                 "transfer_id=%s", self.call_id, tr.transfer_id)
                # Initiated first, as the Gateway does: the FSM only accepts a
                # failure from TRANSFERRING, and the attempt gets counted.
                self._grpc_write_queue.put_nowait(pb.GatewayMessage(transfer_initiated=pb.TransferInitiated(
                    session_id=tr.session_id,
                    transfer_type=tr.transfer_type,
                    destination=tr.destination,
                    reason=tr.reason,
                    transfer_id=tr.transfer_id,
                )))
                self._grpc_write_queue.put_nowait(pb.GatewayMessage(transfer_failed=pb.TransferFailed(
                    session_id=tr.session_id,
                    destination=tr.destination,
                    reason="unsupported_provider",
                    transfer_id=tr.transfer_id,
                )))
            elif which == "end_call":
                self.log.info("end_call reason=%s call=%s", msg.end_call.reason, self.call_id)
                return

    async def _playback_pacer(self, ws) -> None:
        """Drain the play queue at real time (~100ms lead) for the life of the call."""
        while True:
            frame = await self._play_queue.get()
            if frame is _FINAL_MARKER:
                self._playing_tts = False
                self._playback_started_at = None
                self._frames_sent = 0
                self._resolve_turn()
                self._send_playback_finished(interrupted=False)
                continue

            self._playing_tts = True
            now = time.monotonic()
            if self._playback_started_at is None:
                self._playback_started_at = now - _PLAYBACK_LEAD_S
                self._frames_sent = 0
            target = self._playback_started_at + self._frames_sent * (_ULAW_FRAME_BYTES / 8000)
            if target > now:
                await asyncio.sleep(target - now)
            self._frames_sent += 1

            await ws.send_text(self._serializer.play_frame(self._stream_id, frame))
