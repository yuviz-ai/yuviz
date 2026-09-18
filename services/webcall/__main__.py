"""
Webcall bridge — lets a browser tab test a specific agent's live voice
pipeline (STT -> LLM -> tools -> TTS) directly, with zero Gateway,
FreeSWITCH, Kamailio, or carrier involvement.

Why this exists: Conversation Service's Converse RPC already resolves an
agent by (tenant slug, agent slug) alone — SessionOpenRequest.caller_did/
called_did are documented as "informational; may be empty in tests" (see
proto/voiceai/v1/conversation.proto), and __main__.py's handler_factory
calls resolve_handler_deps(ctx.tenant_id, ctx.script_id, ...) using
those two fields as slugs, completely independent of DID-based routing.
So a session that sets tenant_id=<tenant slug>, script_id=<agent slug>,
and leaves caller_did/called_did blank resolves the exact right agent
with no code changes anywhere in Conversation Service itself.

The one real gap: browsers can't speak native gRPC, and gRPC-Web's
support for genuine bidirectional streaming (this RPC sends AudioChunks
and receives TtsChunks concurrently) is a known weak spot depending on
browser/library combination — not something to build the first test
tool's reliability on. This bridge sidesteps that entirely: it's a plain
WebSocket server the browser talks to (binary frames = raw PCM16 audio in
both directions, text frames = small JSON control messages), which opens
a real native gRPC Converse stream to Conversation Service on the
browser's behalf and shuttles messages between the two — the same
WS<->gRPC bridging role the C++ Gateway plays for real telephony calls,
just scoped down to one Python process with no telephony concerns at all.

Audio contract (matches AUDIO_CODEC_PCM_S16LE in the proto exactly):
16-bit signed PCM, little-endian, 16000 Hz, mono. The browser side is
responsible for resampling/converting its mic capture to this format
before sending — see components/TestAgentPanel.tsx.

Text chat (?mode=text) skips the audio contract entirely: the browser sends
{"type":"text_input","text":...} and receives {"type":"agent_text",...},
and Conversation Service runs the turn with STT and TTS switched off (see
SessionOpenRequest.text_only). Everything between — the workflow graph, its
transitions, per-node tools, knowledge retrieval, extraction, end-call and
transfer handling — is the identical code a voice call runs, which is the
only reason testing a workflow this way is worth anything.

v1 is push-to-talk, not continuous VAD: the browser sends a
{"type":"speech_ended"} control message when the caller releases the
talk button, which this bridge turns directly into a
SpeechEndedNotification. Reimplementing the Gateway's VAD logic in the
browser is real work with no clear payoff for a test tool — see the
admin-facing "memory not supported in Webcall"-style disclosed-limitation
precedent from a competitor's own test-call UI.
"""

from __future__ import annotations

# Make generated proto stubs importable as "voiceai.v1.*" (absolute package)
# — same shim services/conversation/__main__.py uses, required because
# conversation_pb2_grpc.py itself imports "from voiceai.v1 import
# conversation_pb2" rather than a fully-qualified package path. Must happen
# before any import that transitively pulls in conversation_pb2_grpc.
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


# Same fence as services/config/deps.CONSOLE_ROLES — supervisor/agent JWTs
# must not open webcall sessions (lesson 4).
_CONSOLE_ROLES = frozenset({"superadmin", "admin", "viewer"})


async def _config_tenant_check(token: str, tenant_slug: str) -> str | None:
    """None if Config would allow GET /tenants/{slug}; else an operator message."""
    base = os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000").rstrip("/")
    url = f"{base}/tenants/{tenant_slug}"

    def _get() -> int | None:
        """HTTP status, or None on transport failure."""
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return int(resp.getcode())
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except Exception:
            log.exception("webcall: Config tenant check transport failure tenant=%s", tenant_slug)
            return None

    status = await asyncio.to_thread(_get)
    if status == 200:
        return None
    if status is None:
        return "could not verify tenant access — is Config Service reachable?"
    if status in (401, 403, 404):
        return "session is not allowed for this tenant"
    log.warning("webcall: Config tenant check status=%s tenant=%s", status, tenant_slug)
    return "could not verify tenant access — is Config Service reachable?"


async def _session_auth_problem(token: str | None, tenant_slug: str) -> tuple[str | None, str | None]:
    """Gate every webcall session: console role + Config tenant isolation.

    Returns ``(error, user_id)``. ``user_id`` is set only when auth succeeds.
    """
    secret = os.environ.get("JWT_SECRET", "").strip()
    if not secret:
        return "webcall auth is unavailable (JWT_SECRET not set on webcall)", None
    if not token:
        return "webcall requires a signed-in session", None
    try:
        import jwt
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return "webcall token is invalid or expired", None
    if payload.get("is_service_account"):
        return "webcall requires an interactive admin session", None
    if payload.get("role") not in _CONSOLE_ROLES:
        return "webcall is not allowed for this account", None
    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        return "webcall token is missing a user id", None
    problem = await _config_tenant_check(token, tenant_slug)
    if problem:
        return problem, None
    return None, user_id


async def _await_session_auth(ws: ServerConnection, tenant_slug: str) -> tuple[str | None, str | None]:
    """First WS text frame must be {"type":"auth","token":...} — never put JWT in the URL.

    Returns ``(error, user_id)``.
    """
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    except asyncio.TimeoutError:
        return "webcall timed out waiting for auth", None
    if not isinstance(raw, str):
        return "webcall expected an auth text frame", None
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return "webcall auth frame was not JSON", None
    if msg.get("type") != "auth":
        return "webcall requires an auth frame first", None
    problem, user_id = await _session_auth_problem(msg.get("token"), tenant_slug)
    if problem:
        return problem, None
    await ws.send(json.dumps({"type": "auth_ok"}))
    return None, user_id


# Back-compat aliases (tests may still import the draft_* names).
_draft_auth_problem = _session_auth_problem
_await_draft_auth = _await_session_auth


def _write_wav_dump(session_id: str, utterance_num: int, pcm: bytes) -> None:
    """Debug aid only, opt-in via WEBCALL_DUMP_AUDIO_DIR: writes each
    utterance's raw audio to a WAV file so it can actually be listened to.
    Every test run so far (synthetic sine tone, Chromium's fake-audio
    device) exercised the WS<->gRPC plumbing but never proved the browser's
    AudioWorklet PCM conversion produces intelligible speech — this is how
    that gets checked against a real human voice instead of guessed at."""
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
    """Reads frames from the browser and writes them onto the gRPC stream.
    Binary frames are raw PCM16 audio; text frames are small JSON control
    messages (speech_ended, cancel, playback_finished)."""
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
        if kind == "text_input":
            text = str(control.get("text", "")).strip()
            if not text:
                continue
            # Do not arm here — the servicer may still be in greeting or may
            # refuse non-text_only sessions. Arm when stt_result echoes back
            # (see ResponseWatchdog.saw), proving the turn was accepted.
            await call.write(pb.GatewayMessage(text_input=pb.TextInput(
                session_id=session_id, text=text,
            )))
        elif kind == "speech_ended":
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
    """Conversation Service silently drops a turn with no reply at all when
    STT transcribes empty text (pipeline.py: 'if not stt_result.text: ...
    return') — correct for real telephony (nothing said -> stay listening),
    but with no timeout on this side, the browser UI would wait forever for
    a message that will never arrive. Arm this right after forwarding
    speech_ended; disarm it the moment any ServiceMessage actually arrives.
    If it fires, the caller genuinely got no response of any kind within
    the timeout, and the browser should say so instead of hanging on
    'Thinking...' indefinitely."""

    def __init__(
        self, ws: ServerConnection, timeout_s: float | None = None, text_mode: bool = False,
    ) -> None:
        # 8s suits a GPU or a hosted LLM. CPU inference is far slower — a 3B
        # model on CPU takes ~17s per turn, so the default fires mid-think and
        # tells the browser "no response" for a reply that is still coming
        # (confirmed live 2026-08-28). Raise it with WEBCALL_RESPONSE_TIMEOUT_S
        # rather than sitting behind a watchdog that is faster than the stack
        # it is watching.
        if timeout_s is None:
            timeout_s = float(os.environ.get("WEBCALL_RESPONSE_TIMEOUT_S", "8"))
        self._ws = ws
        self._timeout_s = timeout_s
        # The default message blames the microphone, which is nonsense
        # advice for someone who just typed a sentence.
        self._text_mode = text_mode
        self._task: asyncio.Task | None = None

    def arm(self) -> None:
        self.disarm()
        self._task = asyncio.create_task(self._fire())

    # Anything arriving at all proves a *voice* turn isn't stuck: the first
    # thing back is either the STT result or an error, and both mean the
    # pipeline is alive.
    #
    # A text turn is different. The service echoes the caller's own words
    # back as stt_result once it has accepted the turn — that is when we
    # arm. Disarming on stt_result made the watchdog dead; arming on send
    # started the clock before the servicer accepted the input. Only a
    # message that ends the turn disarms.
    _TEXT_TURN_ENDING = frozenset({"agent_text", "end_call", "error", "transfer_request"})

    def saw(self, which: str) -> None:
        if self._text_mode:
            if which == "stt_result":
                self.arm()
                return
            if which not in self._TEXT_TURN_ENDING:
                return
            self.disarm()
            return
        self.disarm()

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
                    f"No response after {self._timeout_s:.0f}s — the model is still "
                    "thinking or is unreachable. Check the agent's LLM provider, or "
                    "raise WEBCALL_RESPONSE_TIMEOUT_S if it is simply slow."
                    if self._text_mode else
                    f"No response after {self._timeout_s:.0f}s — the agent likely didn't "
                    "recognize any speech in that recording (silence, background noise, or "
                    "audio too quiet/unclear). Try again, speaking clearly and a bit louder."
                ),
            }))
            log.info("webcall: sent no_response message to browser")
        except Exception:
            log.exception("webcall: failed to send no_response message")


async def _grpc_to_browser(ws: ServerConnection, call, response_watchdog: ResponseWatchdog) -> None:
    """Reads ServiceMessages from the gRPC stream and forwards them to the
    browser — TTS audio payloads as binary frames, everything else as a
    small JSON text frame."""
    async for msg in call:
        which = msg.WhichOneof("payload")
        response_watchdog.saw(which or "")
        if which == "tts_chunk":
            # A chunk can legitimately carry an empty payload (seen live:
            # the chunk right before is_final) — nothing to actually play,
            # and Web Audio's createBuffer() throws for 0 frames on the
            # browser side, so don't bother forwarding it as a binary frame.
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
        elif which == "agent_text":
            await ws.send(json.dumps({
                "type": "agent_text", "text": msg.agent_text.text,
            }))
        elif which == "transfer_request":
            # A browser test has no human-transfer path to execute, so this
            # is reported, never acted on. It is reported rather than
            # dropped (as it used to be) because `transfer` is a first-class
            # workflow node type: a test that reaches one and shows nothing
            # reads as the stage having done nothing at all. Both panels
            # render it as a note and stop.
            await ws.send(json.dumps({
                "type": "transfer",
                "destination": msg.transfer_request.destination,
                "reason": msg.transfer_request.reason,
            }))
        elif which == "tts_started":
            await ws.send(json.dumps({"type": "tts_started"}))
        elif which == "cancel_ack":
            await ws.send(json.dumps({"type": "cancel_ack"}))
        elif which == "error":
            code = msg.error.code or ""
            if code in ("draft_fallback", "session_note"):
                await ws.send(json.dumps({
                    "type": "note", "text": msg.error.message, "kind": code,
                }))
            else:
                await ws.send(json.dumps({
                    "type": "error", "code": code,
                    "message": msg.error.message, "fatal": msg.error.fatal,
                }))
                if msg.error.fatal:
                    return
        elif which == "workflow_node_changed":
            # Lets the workflow editor highlight the stage the call is
            # actually in, live — the difference between "the transition
            # didn't fire" being a guess and something you watch happen.
            await ws.send(json.dumps({
                "type": "workflow_node", "node_id": msg.workflow_node_changed.node_id,
                "node_name": msg.workflow_node_changed.node_name,
                "node_type": msg.workflow_node_changed.node_type,
            }))
        elif which == "end_call":
            await ws.send(json.dumps({
                "type": "end_call", "reason": msg.end_call.reason,
            }))
        # conversation_finalized: nothing the browser can act on.


async def _handle_connection(ws: ServerConnection) -> None:
    params = _parse_query(ws.request.path)
    tenant_slug = params.get("tenant")
    agent_slug = params.get("agent")
    if not tenant_slug or not agent_slug:
        await ws.close(code=1008, reason="missing tenant/agent query params")
        return

    use_draft = params.get("draft") in ("1", "true")
    # Auth every session — draft=1 is only an extra capability on top.
    problem, user_id = await _await_session_auth(ws, tenant_slug)
    if problem or not user_id:
        log.warning("webcall: refusing session — %s", problem)
        try:
            await ws.send(json.dumps({
                "type": "error", "message": problem or "webcall auth failed", "fatal": True,
            }))
        except Exception:
            pass
        await ws.close(code=1008, reason=(problem or "auth failed")[:120])
        return

    # Default to Envoy's gRPC proxy (config/gateway.yaml uses the same
    # target) so this bridge load-balances across both ConvSvc instances
    # like the C++ Gateway does, instead of pinning every call to :50051 —
    # found live 2026-08-04 during a deployment audit.
    conv_target = os.environ.get("CONVERSATION_SVC_TARGET", "localhost:10000")
    session_id = str(uuid.uuid4())
    log.info(
        "webcall: session=%s tenant=%s agent=%s draft=%s user=%s -> %s",
        session_id, tenant_slug, agent_slug, use_draft, user_id, conv_target,
    )

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
            # ?draft=1 (+ first-frame admin JWT) — exercise an unpublished graph.
            use_workflow_draft=use_draft,
            # ?mode=text — type at the agent instead of talking to it. No
            # audio flows in either direction; STT and TTS are skipped.
            text_only=params.get("mode") == "text",
        )))

        response_watchdog = ResponseWatchdog(ws, text_mode=params.get("mode") == "text")
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
