"""
Provider-agnostic Media Stream <-> Conversation Service bridge — same WS
<-> gRPC shuttling role as services/webcall/__main__.py, adapted for a
continuous-streaming media protocol (Vobiz, Cloudonix) instead of
webcall's push-to-talk model:

  - webcall: browser sends raw PCM16 binary frames + an explicit
    {"type":"speech_ended"} control message when the human tester
    releases a talk button.
  - Media Streams providers: JSON-framed base64 mu-law audio streams
    continuously for the whole call, no equivalent client-driven signal
    exists. Speech start/end is instead detected locally with SileroVAD
    (silero_vad.py), a faithful port of the Gateway's own default VAD, so
    a real inbound/outbound call gets the same "caller stopped talking"
    behavior real telephony already gets from the C++ Gateway. (An
    earlier version of this file used EnergyVAD, vad.py's simple
    amplitude threshold — replaced after confirming live that a real
    Vobiz call's line noise/echo was loud enough to trigger false
    barge-ins, up to -13dB, well above EnergyVAD's -35dB cutoff. SileroVAD
    actually classifies speech vs. non-speech instead of just measuring
    loudness.)

Audio flows: inbound media event (b64 mulaw @8k) -> AudioBridge -> PCM16
@16k -> gRPC AudioChunk. VAD runs on the same PCM16 @16k frames (32ms =
1024 bytes, SileroVAD's fixed window size) to emit SpeechEndedNotification
at the right moment. On the way back: gRPC TtsChunk (PCM16 @16k) ->
AudioBridge -> mulaw @8k -> paced into 20ms frames -> outbound playback
events (encoded by the serializer).

Playback is paced at real time (~100ms lead), never sent as fast as it
arrives from gRPC — confirmed live as a real bug, not a theoretical one:
without pacing, an entire TTS response (synthesized far faster than it
takes to speak) lands in the provider's buffer within milliseconds of the
first gRPC chunk, so by the time the caller actually hears the agent
talking and tries to interrupt, there is nothing left queued for
"clear playback" to clear — every barge-in during real playback silently
no-ops. This is the exact same failure mode already documented for the
Gateway's own PlaybackDrain (project memory "project_bargein_playback_design":
"without pacing the whole response lands in the buffer within ms — cancel_playback
has nothing to clear"). _playback_pacer (below) drains a 20ms-frame queue
at real time, and barge-in drops whatever's still queued instead of
relying on a flag that (without pacing) was already stale by the time it
mattered.

Barge-in mirrors the Gateway's own CallSession::on_speech_started exactly
(gateway/src/session/CallSession.cpp), which distinguishes two cases, both
of which cancel the in-flight turn:
  - Speaking (TTS actively playing) -> cancel + clear the already-queued
    audio immediately (can't recall audio already on the wire).
  - Thinking/Synthesizing (LLM/tool-call in flight, no audio chunk yet)
    -> cancel too ("early barge-in"), just with nothing to clear.
Missing the second case was a real, confirmed bug in an earlier version
of this file: a caller who spoke again before the agent's first TTS
chunk arrived (e.g. right after asking a question, while the LLM/tool
call was still running) had that utterance queued as an entirely new,
independent turn instead of interrupting the one in flight — the
Conversation Service has no way to know two turns should collapse into
one, so it processed both, producing confused, out-of-context replies.
_turn_active tracks "is there a turn in flight at all" (from the moment
speech_ended is sent until it resolves via a final tts_chunk, an error,
end_call, or our own cancel); VAD SpeechStart while it's true always
sends CancelGeneration, and additionally sends "clear playback" if audio
was actually playing. Sent the instant SpeechStart fires, not deferred
until cancel_ack comes back — the ack only exists for the pipeline's own
FSM bookkeeping.

Caller audio to gRPC is paced through a short pre-roll delay buffer
(_AUDIO_DELAY_S), not written to the stream the instant it arrives — a
real, confirmed bug otherwise: our VAD needs ~96ms of sustained speech
before SpeechStart fires, but audio_chunks for those first ~96ms were
already written (and queued server-side) before we decided "this is
speech" and sent CancelGeneration. servicer.py's drain loop treats any
audio_chunk that arrived before it dequeues CancelGeneration as leftover
from the turn being cancelled and discards it (see servicer.py's "trailing
audio_chunk... safe to discard" branch) — so the caller's actual first
words ("I don't" of "I don't know") were silently dropped, and only what
came after ("know.") survived. This mirrors why the Gateway itself needed
a 500ms pre-roll buffer for first-word clipping. Fix: hold outgoing audio
_AUDIO_DELAY_S before it's actually written; on barge-in, write
CancelGeneration first, then flush the still-held pre-roll audio
immediately after, guaranteeing the server sees them in the right order
instead of guessing from arrival order.
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
# _VAD_FRAME_BYTES is SileroVAD's own fixed window size (1024 bytes / 32ms
# @ 16kHz) imported above — was EnergyVAD's 640-byte/20ms frame before the
# switch to a real neural VAD (see silero_vad.py's module docstring for why).
_ULAW_FRAME_BYTES = 160  # 20ms @ 8kHz, 1 byte/sample mu-law
_PLAYBACK_LEAD_S = 0.1   # matches the Gateway PlaybackDrain's kLead

# Held before writing caller audio to gRPC, long enough to cover SileroVAD's
# onset_ms (96ms) with margin — see module docstring's "Caller audio to
# gRPC..." paragraph for why this exists at all.
_AUDIO_DELAY_S = 0.15

# Sentinel placed on the playback queue right after a turn's final TTS
# byte — tells the pacer "once you've sent everything before this, the
# turn is fully drained," without racing the queue's own emptiness against
# the next sentence's chunks still arriving from gRPC.
_FINAL_MARKER = object()

# services/conversation/servicer.py only emits a TtsChunk(is_final=True)
# marker when the turn actually produced spoken output (tts_started_sent);
# a silent turn (tool-call-only response, filtered/empty STT, or a turn
# cancelled mid-flight) sends none at all — confirmed live: without this
# watchdog, _turn_active got stuck True after the first such turn and
# every later, perfectly normal utterance was misread as a barge-in.
# Mirrors webcall/__main__.py's ResponseWatchdog pattern (a bounded
# client-side timeout) rather than changing servicer.py, which the
# Gateway and webcall both also depend on.
_TURN_WATCHDOG_S = 12.0


class MediaStreamBridge:
    """One instance per live call, created when the provider's WebSocket
    connects and torn down when it closes. Provider-specific wire shapes
    (event names, streamId/streamSid, frame JSON) live only behind
    `serializer` — see serializers.py."""

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
        self._turn_generation = 0
        self._turn_watchdog: asyncio.Task | None = None

        self._play_queue: asyncio.Queue = asyncio.Queue()
        self._play_buf = bytearray()       # leftover sub-frame bytes between gRPC chunks
        self._playback_started_at: float | None = None
        self._frames_sent = 0

        # Single-writer queue for everything sent to the gRPC stream, so
        # the pre-roll flush on barge-in (below) can guarantee ordering
        # relative to normally-paced audio_chunks without a race between
        # two different tasks both calling call.write().
        self._grpc_write_queue: asyncio.Queue = asyncio.Queue()
        # (enqueued_at_monotonic, AudioChunk proto) pending their _AUDIO_DELAY_S hold.
        self._audio_delay_buf: collections.deque = collections.deque()

        self._dump_dir = os.environ.get("MEDIA_STREAM_DUMP_DIR")
        self._inbound_wav: wave.Wave_write | None = None
        self._outbound_wav: wave.Wave_write | None = None

    def _open_dumps(self) -> None:
        """Debug aid only, opt-in via MEDIA_STREAM_DUMP_DIR (see
        services/webcall/__main__.py's _write_wav_dump for the same
        pattern): writes the whole call's inbound and outbound PCM16 to
        WAV so it can actually be listened to, rather than per-utterance
        since this bridge streams continuously instead of push-to-talk."""
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
        """Drops every not-yet-sent frame — the barge-in equivalent of the
        Gateway's PlaybackQueue::clear(). The one frame the pacer may
        already be mid-sleep on before sending is the same ~20ms
        unrecallable tail the Gateway itself tolerates."""
        while True:
            try:
                self._play_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._play_buf.clear()
        self._playback_started_at = None
        self._frames_sent = 0

    def _flush_pending_audio_now(self) -> None:
        """Bypasses the normal age-based hold and immediately enqueues every
        still-buffered pre-roll audio_chunk — called right after
        CancelGeneration on barge-in so the server sees them in the
        guaranteed-correct order (cancel first, then the caller's actual
        new words), not first-arrived-first-sent."""
        while self._audio_delay_buf:
            _, msg = self._audio_delay_buf.popleft()
            self._grpc_write_queue.put_nowait(msg)

    async def _grpc_writer_loop(self, call) -> None:
        """The only place that ever calls call.write() — every other method
        enqueues here instead, so relative ordering between audio_chunks and
        cancel_generation/speech_ended is exactly the enqueue order, never a
        race between concurrent writers."""
        while True:
            msg = await self._grpc_write_queue.get()
            await call.write(msg)

    async def _audio_delay_pump(self) -> None:
        """Holds each audio_chunk for _AUDIO_DELAY_S before handing it to
        the writer queue — see module docstring for why. On barge-in,
        _flush_pending_audio_now() drains this buffer directly, bypassing
        the hold."""
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
        # Default to Envoy's gRPC proxy (config/gateway.yaml uses the same
        # target) so this bridge load-balances across both ConvSvc
        # instances like the C++ Gateway does, instead of pinning every
        # call to :50051 — found live 2026-08-04 during a deployment audit.
        conv_target = os.environ.get("CONVERSATION_SVC_TARGET", "localhost:10000")
        session_id = str(uuid.uuid4())
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
                # _playback_pacer, _grpc_writer_loop, and _audio_delay_pump
                # never return on their own — they're only here to run for
                # the life of the call, not coroutines whose completion
                # means anything. Tear everything down the instant either
                # of the two real end-of-call signals (WS closed / gRPC
                # stream ended) fires.
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
            except (WebSocketDisconnect, ConnectionClosed):
                # The provider's own hangup callback already tells us the
                # call ended; a trailing TTS chunk racing the caller's
                # hangup is expected, not a bridge failure.
                self.log.info("caller disconnected mid-stream call=%s", self.call_id)
            except grpc.aio.AioRpcError as exc:
                # A caller hangup that lands while _grpc_to_vobiz is mid
                # `async for msg in call` tears the gRPC stream down from
                # underneath it — surfaces here as CANCELLED, UNAVAILABLE, or
                # INTERNAL "Stream removed (RST_STREAM ...)", never as
                # WebSocketDisconnect/ConnectionClosed (confirmed live: the
                # caller-side WS close doesn't turn into either of those on
                # this path). Same benign hangup, logged the same way — any
                # other status still falls through to the real-failure branch
                # below.
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
                # Held for _AUDIO_DELAY_S (see module docstring) rather than
                # written immediately — _audio_delay_pump() releases it on a
                # timer, or _run_vad() flushes it early on barge-in.
                self._audio_delay_buf.append((time.monotonic(), audio_msg))
                await self._run_vad(ws, session_id, pcm16)
            elif kind == "dtmf":
                digit = self._serializer.dtmf_digit(event)
                if not digit:
                    continue
                self.log.info("dtmf received call=%s", self.call_id)
                # Flush held audio first (see module docstring's _AUDIO_DELAY_S
                # note) so a digit keyed in during the delay window is
                # ordered after the audio recorded at the same instant.
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
                    # CancelGeneration enqueued FIRST, then the pre-roll
                    # audio still held in _audio_delay_buf (the caller's
                    # actual first words that triggered this barge-in) —
                    # guarantees the server sees cancel before those
                    # chunks, instead of discarding them as leftover from
                    # the turn being cancelled (see module docstring).
                    self._grpc_write_queue.put_nowait(pb.GatewayMessage(cancel_generation=pb.CancelGeneration(
                        session_id=session_id,
                    )))
                    self._flush_pending_audio_now()
            elif vad_event == VADEvent.SPEECH_END and self._speaking:
                self._speaking = False
                self._start_turn()
                # energy_db is a purely observational float on this proto
                # message (see services/conversation/event_bus.py — feeds a
                # dashboard event, not any decision logic); SileroVAD has no
                # real dB concept, so its speech probability is carried here
                # instead — nothing downstream depends on this being true dB.
                self._grpc_write_queue.put_nowait(pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(
                    session_id=session_id,
                    duration_ms=self._vad.speech_duration_ms,
                    energy_db=self._vad.last_speech_prob,
                )))

    async def _grpc_to_vobiz(self, ws, call) -> None:
        """Feeds paced-out audio into self._play_queue rather than sending
        it straight to the provider — see module docstring for why sending
        it unpaced silently defeats barge-in."""
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
                # clear playback already went out the instant barge-in was
                # detected (_run_vad), and _turn_active was already
                # cleared there too — this ack is just the pipeline's
                # own FSM bookkeeping, nothing left to do here.
                self.log.debug("cancel_ack call=%s", self.call_id)
            elif which == "error":
                self.log.warning("pipeline error call=%s code=%s message=%s",
                                  self.call_id, msg.error.code, msg.error.message)
                self._clear_playback_queue()
                self._playing_tts = False
                self._resolve_turn()
                if msg.error.fatal:
                    return
            elif which == "end_call":
                self.log.info("end_call reason=%s call=%s", msg.end_call.reason, self.call_id)
                return

    async def _playback_pacer(self, ws) -> None:
        """Drains self._play_queue at real time (~100ms lead), so a
        barge-in's clear-playback actually has unsent audio left to drop —
        see module docstring. Runs for the lifetime of the call; the
        FIRST_COMPLETED wait in run() tears it down when the call ends."""
        while True:
            frame = await self._play_queue.get()
            if frame is _FINAL_MARKER:
                self._playing_tts = False
                self._playback_started_at = None
                self._frames_sent = 0
                self._resolve_turn()
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
