"""
ConversationServicer — gRPC servicer bridging each bidi stream to ConversationSession.

Wire protocol per stream:
  1. Read SessionOpenRequest (first GatewayMessage — must arrive first)
  2. Yield ServiceReady
  3. Loop: read AudioChunk / SpeechEnded / CancelGeneration / PlaybackFinished
  4. Stream closes (StopAsyncIteration) → session.close()

An internal asyncio.Queue decouples gRPC message reading from processing.
This allows cancel_generation messages to be processed between pipeline
stages even while a speech_ended() turn is blocked on the LLM or a tool.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from typing import AsyncGenerator, Awaitable, AsyncIterator, Callable

import grpc
import grpc.aio

from .bounded import abandon, run_bounded
from .event_bus import EventBus, TransferRequested
from .pipeline_config import env_count
from .rejection import RejectedCallHangup
from .session import (
    AgentUnavailable, ConversationSession, IConversationHandler, MAX_UTTERANCE_BYTES,
    RoutingStatus, SessionContext,
)
from .transcript_builder import TranscriptBuilder

from .generated.voiceai.v1 import conversation_pb2 as pb
from .generated.voiceai.v1 import conversation_pb2_grpc as pb_grpc

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0"

# One refusal text for a live session_id, whichever tenant holds it.
_SESSION_IN_USE = "session_id already in use"
_INVALID_SESSION_ID = "invalid session_id"
_MAX_SESSION_ID_LEN = 128
_AT_CAPACITY = "conversation service at capacity"
# Fixed peer-facing texts: exception strings can carry hosts, ports and DSNs, so they stay in the server log.
_INTERNAL_MESSAGE = "internal error"
_REJECTED_MESSAGE = "call rejected"

# Caller input held behind a busy turn: every held message counts toward a byte ceiling.
# Audio stops this far short of the ceiling so the speech_ended that closes an utterance still fits.
_CONTROL_RESERVE_BYTES = 64 * 1024
# The ceiling must leave room for a whole utterance below the reserve, or every frame is dropped.
MAX_BACKLOG_BYTES = env_count(
    "VOICEAI_MAX_BACKLOG_BYTES", 4 * MAX_UTTERANCE_BYTES,
    minimum=MAX_UTTERANCE_BYTES + _CONTROL_RESERVE_BYTES,
)
# Complete utterances held behind a busy turn; 0 leaves only the byte ceiling.
MAX_QUEUED_UTTERANCES = env_count("VOICEAI_MAX_QUEUED_UTTERANCES", 3, minimum=0)
# Floor charged per held non-audio message, so a flood of tiny ones is bounded in count too.
_MIN_CONTROL_COST = 256
_DEFERRED_KINDS = ("speech_ended", "transfer_initiated", "transfer_completed", "transfer_failed")


def _held_cost(msg: pb.GatewayMessage) -> int:
    """Bytes a held message is charged: its wire size, and at least _MIN_CONTROL_COST."""
    return max(msg.ByteSize(), _MIN_CONTROL_COST)


# After a half-close or disconnect, all remaining work (the running turn, deferred utterances
# transcribed only, held audio and transfer messages) shares one deadline, so the session ends in
# about 2 s however much is queued; whatever is still held then is dropped.
_CLOSE_BUDGET_S = 1.8
# Bounded wait for a cancelled turn before it is abandoned.
_TURN_RELEASE_TIMEOUT_S = 0.3
# After a close, how long the running (already cancelled) turn may take to return on its own;
# a turn still in its LLM stage is cut there.
_CLOSE_GRACE_S = 0.2
# Per deferred utterance, how long a close lets its turn run so the pipeline can transcribe and persist it.
_CLOSE_DRAIN_TIMEOUT_S = 1.0


def _valid_session_id(sid: str) -> bool:
    """1..128 printable characters, not all whitespace (str.isprintable() rejects NUL and other controls)."""
    return 0 < len(sid) <= _MAX_SESSION_ID_LEN and sid.isprintable() and bool(sid.strip())


# UNSPECIFIED is a non-gateway client: webcall, vobiz, tests.
_ROUTING_STATUS = {
    pb.ROUTING_STATUS_UNSPECIFIED: RoutingStatus.UNSPECIFIED,
    pb.ROUTING_STATUS_ROUTED:      RoutingStatus.ROUTED,
    pb.ROUTING_STATUS_ROUTED_LKG:  RoutingStatus.ROUTED_LKG,
    pb.ROUTING_STATUS_UNKNOWN:     RoutingStatus.UNKNOWN,
    pb.ROUTING_STATUS_UNAVAILABLE: RoutingStatus.UNAVAILABLE,
}


def _cancel_id(cancel: "pb.CancelGeneration") -> int | None:
    """The gateway's cancel id to echo in CancelAck, or None when it sent none."""
    return cancel.cancel_id if cancel.HasField("cancel_id") else None


def _is_reply_audio(response) -> bool:
    """True when the response carries non-empty reply audio; a filler replays from cache even while
    TTS is down, and a zero-byte payload is silence, so neither is the goodbye."""
    return not response.filler and any(response.tts_payloads)


def _consume_pending_transfer(tr, interrupted: bool, sid: str):
    """Return the held TransferRequest message to send at playback end, or None to drop it.

    Barge-in drops LLM-directive transfers (the caller can ask again) but never
    escalation_threshold ones, which nothing would retrigger."""
    if interrupted and tr.trigger != "escalation_threshold":
        log.info(
            "Converse: pending TransferRequest dropped "
            "(barge-in during acknowledgment) transfer_id=%s session=%s",
            tr.transfer_id, sid,
        )
        return None
    if not tr.destination:
        log.error(
            "Converse: refusing to send TransferRequest with empty destination "
            "transfer_id=%s session=%s — check the agent's transfer_destination "
            "config (Escalation tab)", tr.transfer_id, sid,
        )
        return None
    log.info(
        "Converse: sending TransferRequest type=%s destination=%s transfer_id=%s session=%s",
        tr.transfer_type.value, tr.destination, tr.transfer_id, sid,
    )
    return pb.ServiceMessage(
        transfer_request=pb.TransferRequest(
            session_id=sid,
            transfer_type=tr.transfer_type.value,
            destination=tr.destination,
            reason=tr.reason,
            transfer_id=tr.transfer_id,
            caller_id=tr.caller_id,
            waiting_experience=tr.waiting_experience,
        )
    )


class _Held:
    __slots__ = ("msg", "cost", "group", "live")

    def __init__(self, msg: pb.GatewayMessage, cost: int, group: collections.deque | None):
        self.msg, self.cost, self.group, self.live = msg, cost, group, True


class _Utterance:
    """One held utterance: its audio and, once closed, its speech_ended."""
    __slots__ = ("items", "audio", "warned")

    def __init__(self) -> None:
        self.items: collections.deque[_Held] = collections.deque()
        self.audio = 0        # payload bytes accepted, for the per-utterance cap
        self.warned = False   # one WARNING per truncated or dropped utterance


class _HeldBacklog:
    """FIFO of caller messages held behind a busy turn, bounded by MAX_BACKLOG_BYTES.

    Each utterance keeps the MAX_UTTERANCE_BYTES cap; when the ceiling (or MAX_QUEUED_UTTERANCES)
    needs room, the oldest closed utterance is dropped, leaving held control messages in place.
    A speech_ended first evicts held dtmf, so an utterance boundary is never lost; if it still
    does not fit, its open utterance is dropped with it rather than merged into the next.
    Back-to-back playback_finished collapse into the latest. Dropped entries are skipped lazily
    on popleft (and compacted once they outnumber the live ones), so every operation is O(1)
    amortised. Each truncated utterance logs one WARNING; dropped utterances, refusals and dtmf
    evictions log once per busy turn (until the next popleft)."""

    def __init__(self, sid: str) -> None:
        self._sid = sid
        self._items: collections.deque[_Held] = collections.deque()
        self._closed: collections.deque[_Utterance] = collections.deque()
        self._open = _Utterance()
        self._dtmf: collections.deque[_Held] = collections.deque()
        self._tail: _Held | None = None
        self._live = 0
        self.queued_bytes = 0
        self._dead = 0      # dropped entries still in _items, compacted once they outnumber the live
        self._dropped = 0   # utterances dropped this busy turn
        self._refused_audio = 0  # frames refused at the ceiling this busy turn
        self._refuse_warned = self._evict_warned = False

    def __len__(self) -> int:
        return self._live

    def __bool__(self) -> bool:
        return self._live > 0

    def extend(self, msgs) -> None:
        for msg in msgs:
            self.append(msg)

    def append(self, msg: pb.GatewayMessage) -> None:
        if not self._live:
            self._items.clear()
            self._dead = 0
            self._open = _Utterance()  # nothing held: the next utterance starts
        kind = msg.WhichOneof("payload")
        if kind == "audio_chunk":
            self._append_audio(msg)
            return
        cost = _held_cost(msg)
        tail = self._tail
        if (
            kind == "playback_finished" and tail is not None and tail.live
            and tail.msg.HasField("playback_finished")
            and self.queued_bytes - tail.cost + cost <= MAX_BACKLOG_BYTES
        ):
            # Back-to-back playback_finished are redundant: the latest carries the state that matters.
            self.queued_bytes += cost - tail.cost
            tail.msg, tail.cost = msg, cost
            return
        if kind == "speech_ended":
            evicted = 0
            while self._dtmf and self.queued_bytes + cost > MAX_BACKLOG_BYTES:
                self._kill(self._dtmf.popleft())
                evicted += 1
            if evicted and not self._evict_warned:
                self._evict_warned = True
                log.warning(
                    "Converse: caller backlog full, dropped %d oldest held dtmf to keep an "
                    "utterance boundary session=%s", evicted, self._sid,
                )
            while self._closed and self.queued_bytes + cost > MAX_BACKLOG_BYTES:
                self._drop(self._closed.popleft())
            if self.queued_bytes + cost > MAX_BACKLOG_BYTES:
                self._drop(self._open)  # unclosed, its audio would merge into the next utterance
                self._open = _Utterance()
                return
            utterance, self._open = self._open, _Utterance()
            self._store(msg, cost, utterance.items)
            self._closed.append(utterance)
            while MAX_QUEUED_UTTERANCES and len(self._closed) > MAX_QUEUED_UTTERANCES:
                self._drop(self._closed.popleft())
            return
        if self.queued_bytes + cost > MAX_BACKLOG_BYTES:
            if not self._refuse_warned:  # a control flood must not also flood the log
                self._refuse_warned = True
                log.warning("Converse: caller backlog full, dropping %s session=%s", kind, self._sid)
            return
        evictable = kind not in _DEFERRED_KINDS and kind != "playback_finished"
        self._store(msg, cost, self._dtmf if evictable else None)

    def popleft(self) -> pb.GatewayMessage:
        entry = self._items.popleft()
        while not entry.live:
            self._dead -= 1
            entry = self._items.popleft()
        entry.live = False
        self._live -= 1
        self.queued_bytes -= entry.cost
        if entry.group is not None:
            entry.group.popleft()  # groups pop in arrival order, so this is the entry
        if entry.msg.HasField("speech_ended"):
            self._closed.popleft()  # its utterance is now empty
        self.end_busy_turn()
        return entry.msg

    def end_busy_turn(self) -> None:
        """Log the busy turn's drop totals (beyond the first WARNING) and re-arm the once-per-turn logs."""
        if self._dropped > 1 or self._refused_audio > 1:
            log.info(
                "Converse: %d queued utterances and %d audio frames dropped during the busy turn session=%s",
                self._dropped, self._refused_audio, self._sid,
            )
        self._dropped = self._refused_audio = 0
        self._refuse_warned = self._evict_warned = False

    def _append_audio(self, msg: pb.GatewayMessage) -> None:
        utterance = self._open
        cost = _held_cost(msg)
        limit = MAX_BACKLOG_BYTES - _CONTROL_RESERVE_BYTES
        if utterance.audio < MAX_UTTERANCE_BYTES:  # over the utterance cap: dropped, as push_audio would
            while self._closed and self.queued_bytes + cost > limit:
                self._drop(self._closed.popleft())
            if self.queued_bytes + cost <= limit:
                utterance.audio += len(msg.audio_chunk.payload)
                self._store(msg, cost, utterance.items)
                return
            # No room under the ceiling: once per busy turn, or a control flood that fills the
            # backlog turns every later tiny utterance into a log line.
            self._refused_audio += 1
            if self._refused_audio == 1:
                log.warning("Converse: caller backlog full, dropping audio session=%s", self._sid)
            return
        if not utterance.warned:  # over its own cap: once per utterance
            utterance.warned = True
            log.warning(
                "Converse: queued utterance over limit, dropping excess buffered=%d session=%s",
                utterance.audio, self._sid,
            )

    def _store(self, msg: pb.GatewayMessage, cost: int, group: collections.deque | None) -> None:
        entry = _Held(msg, cost, group)
        self._items.append(entry)
        if group is not None:
            group.append(entry)
        self._tail = entry
        self._live += 1
        self.queued_bytes += cost

    def _kill(self, entry: _Held) -> None:
        entry.live = False
        entry.msg = entry.group = None  # the dead entry waits in _items; its payload must not
        self._live -= 1
        self.queued_bytes -= entry.cost
        self._dead += 1
        if self._dead > self._live:
            self._items = collections.deque(e for e in self._items if e.live)
            self._dead = 0

    def _drop(self, utterance: _Utterance) -> None:
        """Drop an utterance's held audio and speech_ended; control messages stay. The first drop
        of a busy turn logs a WARNING; the turn's total is logged when the backlog starts draining."""
        for entry in utterance.items:
            self._kill(entry)
        utterance.items.clear()
        self._dropped += 1
        if self._dropped == 1:
            log.warning(
                "Converse: caller backlog over limit, dropped oldest queued utterance session=%s", self._sid,
            )


class ConversationServicer(pb_grpc.ConversationServiceServicer):
    """Stateless servicer: a fresh ConversationSession and handler per Converse() call."""

    def __init__(
        self,
        handler_factory: Callable[[SessionContext], Awaitable[IConversationHandler]],
        transcripts: TranscriptBuilder | None = None,
        max_sessions: int = 0,
        on_capacity_change: Callable[[bool], None] | None = None,
    ) -> None:
        # max_sessions <= 0 means no cap. A new call over the cap is refused with RESOURCE_EXHAUSTED
        # so the gateway can retry another replica instead of degrading every live call here.
        self._max_sessions = max_sessions
        self._on_capacity_change = on_capacity_change
        self._handler_factory = handler_factory
        self._transcripts = transcripts
        self._stream_tasks: set[asyncio.Task] = set()
        self._live_sessions: set[str] = set()

    async def wait_closed(self, timeout: float) -> None:
        """Wait for open streams to finish their teardown, so end-of-call writes are queued
        before the caller closes the transcript pool. Call after the server has stopped."""
        if self._stream_tasks:
            await asyncio.wait(self._stream_tasks, timeout=timeout)

    async def Converse(
        self,
        request_iterator: AsyncIterator[pb.GatewayMessage],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb.ServiceMessage]:

        stream_task = asyncio.current_task()
        self._stream_tasks.add(stream_task)
        stream_task.add_done_callback(self._stream_tasks.discard)

        # ── 1. Handshake ───────────────────────────────────────────────────────
        try:
            first = await request_iterator.__anext__()
        except StopAsyncIteration:
            log.warning("Stream closed before SessionOpenRequest")
            return

        if not first.HasField("session_open"):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "First message must be session_open",
            )
            return

        open_req = first.session_open
        client_ver = open_req.protocol_version

        if not client_ver.startswith("1."):
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                f"Unsupported protocol_version={client_ver!r}; server speaks 1.x",
            )
            return

        # Refuse an unusable id before anything is keyed by it: an empty id would be one platform-wide
        # id, NUL/control characters break the call-row write and forge log lines, a huge id is
        # stored and repeated in every log line. The id is never echoed back or logged.
        if not _valid_session_id(open_req.session_id):
            log.warning("Converse: invalid session_id refused (len=%d) tenant=%s",
                        len(open_req.session_id), open_req.tenant_id)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, _INVALID_SESSION_ID)
            return

        # Per-session state downstream (transcript bookkeeping, STT stream, call row) is keyed by the
        # client-supplied session_id, so one live stream per id, across tenants. The refusal text is
        # the same whichever tenant holds the id.
        sid = open_req.session_id
        if sid in self._live_sessions:
            log.warning("Converse: duplicate live session_id refused session=%s tenant=%s",
                        sid, open_req.tenant_id)
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, _SESSION_IN_USE)
            return
        if self._max_sessions > 0 and len(self._live_sessions) >= self._max_sessions:
            log.warning("Converse: at capacity (%d live sessions), refused session=%s",
                        len(self._live_sessions), sid)
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, _AT_CAPACITY)
            return
        self._live_sessions.add(sid)
        self._notify_capacity()
        try:
            async with contextlib.aclosing(self._serve(open_req, request_iterator)) as stream:
                async for message in stream:
                    yield message
        finally:
            # end_call() normally cleared these; a teardown cut short before it must not leave them behind.
            if self._transcripts is not None:
                self._transcripts.discard_session(sid)
            self._live_sessions.discard(sid)
            self._notify_capacity()

    def _notify_capacity(self) -> None:
        if self._on_capacity_change is not None and self._max_sessions > 0:
            self._on_capacity_change(len(self._live_sessions) >= self._max_sessions)

    async def _serve(
        self,
        open_req: pb.SessionOpenRequest,
        request_iterator: AsyncIterator[pb.GatewayMessage],
    ) -> AsyncIterator[pb.ServiceMessage]:
        sid = open_req.session_id
        routing_status = _ROUTING_STATUS.get(open_req.routing_status)
        if routing_status is None:
            # A status this build doesn't know must not fail open to a default agent.
            log.warning("Converse: unknown routing_status=%d — treating as unavailable",
                        open_req.routing_status)
            routing_status = RoutingStatus.UNAVAILABLE
        log.info("Converse: session_open session=%s tenant=%s routing=%s",
                 sid, open_req.tenant_id, routing_status.value)

        ctx = SessionContext(
            session_id=sid,
            tenant_id=open_req.tenant_id,
            trace_id=open_req.trace_id,
            call_id=open_req.call_id,
            caller_did=open_req.caller_did,
            called_did=open_req.called_did,
            direction=open_req.direction,
            script_id=open_req.script_id,
            test_credential=open_req.test_credential,
            routing_status=routing_status,
        )

        bus     = EventBus()
        try:
            handler = await self._handler_factory(ctx)  # fresh instance per stream, agent-aware
        except AgentUnavailable:
            yield pb.ServiceMessage(
                error=pb.ServiceError(
                    session_id=sid,
                    code="AGENT_UNAVAILABLE",
                    message="agent unavailable",
                    fatal=True,
                )
            )
            return
        except Exception:
            log.exception("Converse: handler factory failed session=%s", sid)
            yield pb.ServiceMessage(
                error=pb.ServiceError(
                    session_id=sid,
                    code="INTERNAL",
                    message=_INTERNAL_MESSAGE,
                    fatal=True,
                )
            )
            return
        session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
        session.session_ready()
        await bus.start()

        # ── 2. ServiceReady ────────────────────────────────────────────────────
        yield pb.ServiceMessage(
            service_ready=pb.ServiceReady(
                session_id=sid,
                protocol_version=PROTOCOL_VERSION,
            )
        )

        tts_seq = 0

        # ── 3. Start reader before greeting ────────────────────────────────────
        # Read during greeting synthesis, or the gateway's gRPC send buffer fills and
        # its write fails. Unbounded so greeting-time audio is buffered.
        msg_q: asyncio.Queue[pb.GatewayMessage | None] = asyncio.Queue()

        async def _reader() -> None:
            try:
                async for msg in request_iterator:
                    await msg_q.put(msg)
            except Exception:
                log.debug("Converse: reader task exited session=%s", sid)
            finally:
                await msg_q.put(None)

        reader_task = asyncio.create_task(_reader())

        # Held until playback_finished: the gateway transfers immediately on
        # TransferRequest and would cut off the spoken acknowledgment.
        pending_transfer = None
        transfer_started_at: float | None = None

        async def _emit_response(
            response, end_reason: str = "agent_ended_call",
        ) -> AsyncIterator[pb.ServiceMessage]:
            """Emit an out-of-band HandlerResponse (e.g. call-flow timeout).

            Always sends the terminal empty chunk, even with no TTS, or the gateway never consumes EndCall."""
            nonlocal tts_seq, pending_transfer

            yield pb.ServiceMessage(tts_started=pb.TtsStarted(session_id=sid))
            for payload_bytes in response.tts_payloads:
                tts_seq += 1
                yield pb.ServiceMessage(
                    tts_chunk=pb.TtsChunk(
                        session_id=sid,
                        sequence_num=tts_seq,
                        codec=pb.AUDIO_CODEC_PCM_S16LE,
                        sample_rate=16000,
                        payload=payload_bytes,
                        is_final=False,
                    )
                )
            tts_seq += 1
            yield pb.ServiceMessage(
                tts_chunk=pb.TtsChunk(
                    session_id=sid,
                    sequence_num=tts_seq,
                    codec=pb.AUDIO_CODEC_PCM_S16LE,
                    sample_rate=16000,
                    payload=b"",
                    is_final=True,
                )
            )

            if response.end_call:
                yield pb.ServiceMessage(
                    end_call=pb.EndCall(
                        session_id=sid,
                        reason=end_reason,
                        grace_period_ms=response.end_call_grace_period_ms,
                    )
                )

            if response.transfer_request:
                tr = response.transfer_request
                # TTS was always sent above, so always wait for its playback.
                pending_transfer = tr
                log.info(
                    "Converse: TransferRequest held until playback finishes "
                    "(out-of-band) session=%s", sid,
                )

        # ── 2b. Greeting ───────────────────────────────────────────────────────
        async def _greet() -> AsyncIterator[pb.ServiceMessage]:
            nonlocal tts_seq
            greeting_end_call = None
            async for response in session.greet():
                if response.end_call:
                    greeting_end_call = response   # sent below via _emit_response
                    continue
                if response.tts_payloads:
                    yield pb.ServiceMessage(
                        tts_started=pb.TtsStarted(session_id=sid)
                    )
                    for i, payload in enumerate(response.tts_payloads):
                        tts_seq += 1
                        yield pb.ServiceMessage(
                            tts_chunk=pb.TtsChunk(
                                session_id=sid,
                                sequence_num=tts_seq,
                                codec=pb.AUDIO_CODEC_PCM_S16LE,
                                sample_rate=16000,
                                payload=payload,
                                is_final=(i == len(response.tts_payloads) - 1),
                            )
                        )

            if greeting_end_call is not None:
                log.info("Converse: rejecting call routing=%s session=%s",
                         ctx.routing_status.value, sid)
                async for out in _emit_response(
                    greeting_end_call, end_reason=f"routing_{ctx.routing_status.value}",
                ):
                    yield out

        msg_fut: asyncio.Future | None = None
        out_fut: asyncio.Future | None = None
        turn = None
        apology = None

        # Messages taken off msg_q while a turn runs, ahead of anything still queued.
        backlog: collections.deque[pb.GatewayMessage | None] = collections.deque()
        # Once a speech_ended / transfer_* is deferred to the outer loop, everything behind it
        # belongs to later utterances and waits here, ahead of `backlog`, until the outer loop
        # reaches it. A turn's drain never pops from it, so a deferred message is examined once,
        # not again by each later turn (which would discard the next utterance's audio as
        # "trailing frames").
        # Bounded (see _HeldBacklog) so a flood can't pile up in memory while a slow turn runs.
        held = _HeldBacklog(sid)
        turn_stream: AsyncGenerator | None = None
        # Set once the gateway stream has ended: later turns only transcribe, and each is bounded.
        closing = False
        close_deadline = 0.0                 # loop.time() by which all post-close work is done
        turn_deadline: float | None = None   # loop.time() at which the running turn is released
        loop = asyncio.get_running_loop()

        def _begin_close() -> None:
            nonlocal closing, close_deadline
            if not closing:
                closing = True
                close_deadline = loop.time() + _CLOSE_BUDGET_S

        def _close_left() -> float:
            return max(0.0, close_deadline - loop.time())

        async def _turn_responses(turn_iter: AsyncIterator) -> AsyncGenerator:
            """Yield the turn's responses, or None whenever a gateway message arrives first,
            so a cancel is seen while the turn is blocked on the LLM or a tool.
            Returns once `turn_deadline` passes."""
            nonlocal turn
            next_resp = asyncio.ensure_future(turn_iter.__anext__())
            next_msg: asyncio.Future | None = None
            try:
                while True:
                    if next_msg is None:
                        next_msg = asyncio.ensure_future(msg_q.get())
                    done, _ = await asyncio.wait(
                        {next_resp, next_msg},
                        timeout=None if turn_deadline is None else max(0.0, turn_deadline - loop.time()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        return
                    if next_msg.done():
                        backlog.append(next_msg.result())
                        next_msg = None
                        yield None
                    elif next_resp.done():
                        try:
                            response = next_resp.result()
                        except StopAsyncIteration:
                            return
                        next_resp = asyncio.ensure_future(turn_iter.__anext__())
                        yield response
            finally:
                if next_msg is not None and not next_msg.cancel():
                    backlog.append(next_msg.result())
                if next_resp.done():
                    abandon(next_resp)  # reads an outcome nobody consumed
                else:
                    next_resp.cancel()
                    try:
                        released, _ = await asyncio.wait(
                            {next_resp},
                            timeout=min(_TURN_RELEASE_TIMEOUT_S, _close_left()) if closing
                            else _TURN_RELEASE_TIMEOUT_S,
                        )
                    finally:
                        abandon(next_resp)  # cancels again a turn that swallowed the first
                    if not released:
                        log.warning("Converse: turn ignored its cancellation and was abandoned session=%s", sid)
                        # Still running: teardown's turn.aclose() would raise "already running".
                        turn = None

        try:
            # Inside the try: a hangup while the greeting is still synthesizing
            # cancels the stream here, and the session must still be closed.
            async for out in _greet():
                yield out

            while True:
                if not closing and reader_task.done():
                    _begin_close()  # the stream ended, even if no turn drained its end
                if closing and not _close_left():
                    # The stream's end marker (None) is not counted: it sits in backlog, in a finished
                    # msg_fut, or still in msg_q.
                    taken = list(backlog)
                    if msg_fut is not None and msg_fut.done() and not msg_fut.cancelled():
                        taken.append(msg_fut.result())
                    left = (
                        len(held) + sum(m is not None for m in taken)
                        + msg_q.qsize() - (not any(m is None for m in taken))
                    )
                    if left > 0:
                        log.warning(
                            "Converse: close deadline reached, dropping %d held caller messages session=%s",
                            left, sid,
                        )
                    break
                if held:
                    msg = held.popleft()
                elif backlog:
                    msg = backlog.popleft()
                elif session.out_responses is not None:
                    if msg_fut is None:
                        msg_fut = asyncio.ensure_future(msg_q.get())
                    if out_fut is None:
                        out_fut = asyncio.ensure_future(session.out_responses.get())
                    done, _ = await asyncio.wait(
                        {msg_fut, out_fut}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if out_fut in done:
                        out_response = out_fut.result()
                        out_fut = None
                        async for out_msg in _emit_response(out_response):
                            yield out_msg
                        continue  # msg_fut (if any) stays pending for next iteration
                    msg = msg_fut.result()
                    msg_fut = None
                else:
                    msg = await msg_q.get()

                if msg is None:
                    break  # gRPC stream closed

                payload_case = msg.WhichOneof("payload")

                if payload_case == "audio_chunk" and closing:
                    # Only feeds a deferred utterance's transcript now; nothing is sent.
                    with contextlib.suppress(TimeoutError):
                        await run_bounded(
                            session.push_audio(msg.audio_chunk.payload, trace_id=msg.audio_chunk.trace_id),
                            _close_left(),
                        )

                elif payload_case == "audio_chunk":
                    chunk    = msg.audio_chunk
                    response = await session.push_audio(
                        chunk.payload, trace_id=chunk.trace_id
                    )

                    if response.stt_text:
                        yield pb.ServiceMessage(
                            stt_result=pb.SttResult(
                                session_id=sid,
                                trace_id=chunk.trace_id,
                                text=response.stt_text,
                                confidence=response.stt_confidence,
                                is_final=True,
                            )
                        )

                    if response.tts_payloads:
                        yield pb.ServiceMessage(
                            tts_started=pb.TtsStarted(
                                session_id=sid,
                                trace_id=chunk.trace_id,
                            )
                        )
                        for i, payload in enumerate(response.tts_payloads):
                            tts_seq += 1
                            yield pb.ServiceMessage(
                                tts_chunk=pb.TtsChunk(
                                    session_id=sid,
                                    trace_id=chunk.trace_id,
                                    sequence_num=tts_seq,
                                    codec=pb.AUDIO_CODEC_PCM_S16LE,
                                    sample_rate=16000,
                                    payload=payload,
                                    is_final=(i == len(response.tts_payloads) - 1),
                                )
                            )

                elif payload_case == "speech_ended":
                    se = msg.speech_ended
                    # Stream SttResult/TTS chunks as the pipeline produces them —
                    # buffering the whole turn would trip the gateway's SttTimeout.
                    tts_started_sent      = False
                    audio_sent            = False
                    cancelled             = False
                    stt_sent              = False
                    end_call_requested    = False
                    end_call_without_audio = False
                    end_call_grace_period = 0
                    turn_transfer         = None
                    # Audio seen while no utterance is deferred yet: the next utterance's own
                    # frames if a speech_ended follows, trailing frames of this one if not.
                    pre_audio: collections.deque[pb.GatewayMessage] = collections.deque()
                    pre_audio_bytes = 0

                    # Keep the inner turn: teardown must aclose() it (not only the wrapper) so the
                    # pipeline's GeneratorExit handler records the interrupted turn.
                    turn_deadline = None
                    unexpected_warned = False
                    if closing:
                        # The caller is gone: this deferred utterance is only transcribed (closing the
                        # turn below persists its transcript) and nothing is sent, within its own budget.
                        cancelled = True
                        turn_deadline = min(loop.time() + _CLOSE_DRAIN_TIMEOUT_S, close_deadline)
                    turn = session.speech_ended(se.duration_ms, se.energy_db)
                    turn_stream = _turn_responses(turn.__aiter__())
                    # Behind a deferred message only a cancel or end of stream still stops this turn.
                    async for response in turn_stream:
                        # Drain BEFORE sending so a cancel_generation that arrived
                        # during synthesis stops this turn's audio from being sent.
                        # After the close nothing new arrives, and its None stays for the outer loop.
                        while not closing and (backlog or not msg_q.empty()):
                            pending = backlog.popleft() if backlog else msg_q.get_nowait()
                            if pending is None:
                                # Stream ended during pipeline — stop generating.
                                await session.cancel(hanging_up=True)
                                cancelled = True
                                pre_audio.clear()
                                pre_audio_bytes = 0
                                backlog.appendleft(None)  # so outer loop exits
                                # A handler that ignores the cancel must not hold the session open.
                                _begin_close()
                                turn_deadline = min(loop.time() + _CLOSE_GRACE_S, close_deadline)
                                break
                            pcase = pending.WhichOneof("payload")
                            if held and pcase != "cancel_generation":
                                held.append(pending)
                            elif pcase == "cancel_generation":
                                await session.cancel()
                                cancelled = True
                                # Frames from before the cancel are not part of the barge-in.
                                pre_audio.clear()
                                pre_audio_bytes = 0
                                # Ack now so C++ leaves BargeIn without waiting for
                                # the 500 ms barge_in_window; don't break — let the
                                # generator exhaust to release pipeline resources.
                                yield pb.ServiceMessage(
                                    cancel_ack=pb.CancelAck(
                                        session_id=sid,
                                        trace_id=pending.cancel_generation.trace_id,
                                        cancel_id=_cancel_id(pending.cancel_generation),
                                    )
                                )
                            elif pcase == "playback_finished":
                                pf = pending.playback_finished
                                log.info(
                                    "Converse: playback_finished interrupted=%s (in-pipeline) session=%s",
                                    pf.interrupted, sid,
                                )
                                session.on_playback_finished(
                                    pf.interrupted, pf.played_bytes if pf.HasField("played_bytes") else None,
                                )
                                if pending_transfer is not None:
                                    tr, pending_transfer = pending_transfer, None
                                    out = _consume_pending_transfer(tr, pf.interrupted, sid)
                                    if out is not None:
                                        yield out
                                    elif pf.interrupted:
                                        session.on_transfer_cancelled(tr.transfer_id)
                            elif pcase == "audio_chunk":
                                if cancelled:
                                    # Post-cancel frames are the caller's barge-in
                                    # utterance flushed by the gateway — keep them.
                                    await session.push_audio(
                                        pending.audio_chunk.payload,
                                        trace_id=pending.audio_chunk.trace_id,
                                    )
                                else:
                                    # Either trailing frames of this utterance or the start of the
                                    # next one; only a following speech_ended tells which.
                                    # Bounded to one utterance so a long turn cannot grow it.
                                    pre_audio.append(pending)
                                    pre_audio_bytes += _held_cost(pending)
                                    while pre_audio_bytes > MAX_UTTERANCE_BYTES and pre_audio:
                                        pre_audio_bytes -= _held_cost(pre_audio.popleft())
                            elif pcase == "speech_ended":
                                # New utterance while this turn's pipeline still runs —
                                # leave it for the outer loop, with the audio sent before it.
                                held.extend(pre_audio)
                                pre_audio.clear()
                                pre_audio_bytes = 0
                                held.append(pending)
                            elif pcase in (
                                "transfer_initiated", "transfer_completed", "transfer_failed",
                            ):
                                # Defer: transfer_failed streams its own TTS, which would
                                # interleave with this turn's audio.
                                held.append(pending)
                            elif not unexpected_warned:  # once per turn: a flood must not flood the log
                                unexpected_warned = True
                                log.warning(
                                    "Unexpected %s during speech pipeline session=%s",
                                    pcase, sid,
                                )

                        if response is None:
                            continue  # woken by a message, already handled above
                        if closing and response.stt_text:
                            break  # the transcript persists as the turn closes; a reply would never be heard

                        if not cancelled:
                            if response.end_call:
                                end_call_requested    = True
                                end_call_without_audio = response.end_call_without_audio
                                end_call_grace_period = response.end_call_grace_period_ms
                            if response.transfer_request:
                                tr = response.transfer_request
                                log.info(
                                    "Converse: transfer requested trigger=%s type=%s "
                                    "destination=%s session=%s",
                                    tr.trigger, tr.transfer_type.value, tr.destination, sid,
                                )
                                bus.publish(TransferRequested(
                                    session_id=tr.session_id,
                                    tenant_id=tr.tenant_id,
                                    call_id=tr.call_id,
                                    transfer_type=tr.transfer_type.value,
                                    destination=tr.destination,
                                    reason=tr.reason,
                                    trigger=tr.trigger,
                                ))
                                turn_transfer = tr
                            # Send STT result immediately — advances C++ FSM
                            # Recognizing→Thinking before SttTimeout (8 s) fires.
                            if response.stt_text:
                                stt_sent = True
                                yield pb.ServiceMessage(
                                    stt_result=pb.SttResult(
                                        session_id=sid,
                                        text=response.stt_text,
                                        confidence=response.stt_confidence,
                                        is_final=True,
                                    )
                                )
                            # Stream TTS chunks as each sentence is synthesized —
                            # advances C++ FSM Thinking→Synthesizing→Speaking so
                            # barge-in detection works during playback.
                            if any(response.tts_payloads):
                                audio_sent = audio_sent or _is_reply_audio(response)
                                if not tts_started_sent:
                                    yield pb.ServiceMessage(
                                        tts_started=pb.TtsStarted(session_id=sid)
                                    )
                                    tts_started_sent = True
                                # Past the cancel drain: the chunk below is what the caller is sent.
                                if response.on_sent is not None:
                                    response.on_sent()
                                for payload_bytes in response.tts_payloads:
                                    tts_seq += 1
                                    yield pb.ServiceMessage(
                                        tts_chunk=pb.TtsChunk(
                                            session_id=sid,
                                            sequence_num=tts_seq,
                                            codec=pb.AUDIO_CODEC_PCM_S16LE,
                                            sample_rate=16000,
                                            payload=payload_bytes,
                                            is_final=False,
                                        )
                                    )
                            # Text alongside the audio, for the browser test-call panel.
                            if response.response_text:
                                yield pb.ServiceMessage(
                                    assistant_response=pb.AssistantResponse(
                                        session_id=sid, text=response.response_text,
                                    )
                                )
                    if closing:
                        await turn_stream.aclose()  # releases a turn still running, bounded
                        turn_stream = None
                        with contextlib.suppress(TimeoutError):
                            if turn is not None:  # None when the turn ignored the release and was abandoned
                                await run_bounded(turn.aclose(), _close_left())  # persists a turn closed at a yield
                            await run_bounded(session.cancel(), _close_left())  # a cut-off turn leaves the FSM busy
                        turn = None
                    turn_deadline = None
                    # Empty transcript (noise / filtered hallucination): tell the
                    # gateway so its FSM returns to Listening instead of waiting
                    # out the 8 s SttTimeout in silence.
                    if not cancelled and not stt_sent:
                        yield pb.ServiceMessage(
                            stt_result=pb.SttResult(
                                session_id=sid, text="", confidence=0.0, is_final=True,
                            )
                        )

                    # TTS produced no audio for a transcribed turn (provider failure): still
                    # close the turn, or the gateway waits in silence until the caller speaks again.
                    if stt_sent and not tts_started_sent and not cancelled and turn_transfer is None:
                        yield pb.ServiceMessage(tts_started=pb.TtsStarted(session_id=sid))
                        tts_started_sent = True

                    # End-of-response marker: empty is_final chunk; the gateway
                    # holds playback_finished until the final frame drains.
                    if tts_started_sent and not cancelled:
                        tts_seq += 1
                        yield pb.ServiceMessage(
                            tts_chunk=pb.TtsChunk(
                                session_id=sid,
                                sequence_num=tts_seq,
                                codec=pb.AUDIO_CODEC_PCM_S16LE,
                                sample_rate=16000,
                                payload=b"",
                                is_final=True,
                            )
                        )

                    # Sent after the final TtsChunk so the gateway waits for the goodbye to drain.
                    # Requires reply or goodbye audio this turn (not a filler or the synthetic TtsStarted),
                    # else the flag leaks into a later turn's playback.
                    if end_call_requested and not cancelled and audio_sent:
                        log.info(
                            "Converse: sending EndCall grace_period_ms=%d session=%s",
                            end_call_grace_period, sid,
                        )
                        yield pb.ServiceMessage(
                            end_call=pb.EndCall(
                                session_id=sid,
                                reason="agent_ended_call",
                                grace_period_ms=end_call_grace_period,
                            )
                        )
                    elif end_call_requested and end_call_without_audio and not cancelled:
                        # The max-duration cap fires even when nothing played; the gateway enters the end-call
                        # grace period, where the caller can still cancel by speaking.
                        log.warning(
                            "Converse: max-duration EndCall with no audio session=%s", sid,
                        )
                        yield pb.ServiceMessage(
                            end_call=pb.EndCall(
                                session_id=sid,
                                reason="max_duration_no_audio",
                                grace_period_ms=end_call_grace_period,
                            )
                        )
                    elif end_call_requested and not cancelled:
                        log.warning(
                            "Converse: end-call marker detected but turn produced no "
                            "TTS audio — not sending EndCall (would leak into a later "
                            "turn on the gateway side) session=%s", sid,
                        )

                    # Without TTS no playback_finished will come, so send immediately.
                    if turn_transfer is not None and not cancelled:
                        if tts_started_sent:
                            pending_transfer = turn_transfer
                            log.info(
                                "Converse: TransferRequest held until playback "
                                "finishes session=%s", sid,
                            )
                        else:
                            log.info(
                                "Converse: TransferRequest dispatching immediately "
                                "(no TTS this turn) transfer_id=%s session=%s",
                                turn_transfer.transfer_id, sid,
                            )
                            out = _consume_pending_transfer(turn_transfer, False, sid)
                            if out is not None:
                                yield out

                elif payload_case == "dtmf":
                    # Never log the digit value.
                    log.info("Converse: dtmf session=%s", sid)
                    await session.push_dtmf(msg.dtmf.digit)

                elif payload_case == "cancel_generation":
                    await session.cancel()
                    yield pb.ServiceMessage(
                        cancel_ack=pb.CancelAck(
                            session_id=sid,
                            trace_id=msg.cancel_generation.trace_id,
                            cancel_id=_cancel_id(msg.cancel_generation),
                        )
                    )

                elif payload_case == "playback_finished":
                    pf = msg.playback_finished
                    log.info("Converse: playback_finished interrupted=%s session=%s",
                             pf.interrupted, sid)
                    session.on_playback_finished(
                        pf.interrupted, pf.played_bytes if pf.HasField("played_bytes") else None,
                    )
                    if pending_transfer is not None:
                        tr, pending_transfer = pending_transfer, None
                        out = _consume_pending_transfer(tr, pf.interrupted, sid)
                        if out is not None:
                            yield out
                        elif pf.interrupted:
                            session.on_transfer_cancelled(tr.transfer_id)

                elif payload_case == "transfer_initiated":
                    ti = msg.transfer_initiated
                    transfer_started_at = time.monotonic()
                    log.info("Converse: transfer_initiated type=%s destination=%s "
                             "transfer_id=%s session=%s",
                             ti.transfer_type, ti.destination, ti.transfer_id, sid)
                    session.on_transfer_initiated(ti.transfer_type, ti.destination, ti.reason,
                                                  ti.transfer_id)

                # The gateway's Finalizing state waits on ConversationFinalized.
                elif payload_case == "transfer_completed":
                    tc = msg.transfer_completed
                    duration_ms = (
                        int((time.monotonic() - transfer_started_at) * 1000)
                        if transfer_started_at is not None else -1
                    )
                    log.info("Converse: transfer_completed destination=%s transfer_id=%s "
                             "duration_ms=%d session=%s",
                             tc.destination, tc.transfer_id, duration_ms, sid)
                    try:
                        result = await (
                            run_bounded(session.on_transfer_completed(tc.destination, tc.transfer_id), _close_left())
                            if closing else session.on_transfer_completed(tc.destination, tc.transfer_id)
                        )
                    except TimeoutError:
                        log.warning("Converse: transfer_completed not finalized by the close deadline session=%s", sid)
                        continue
                    if result is None:
                        continue  # no transfer was pending; ignored
                    log.info(
                        "Converse: conversation_finalized session=%s summary_generated=%s "
                        "transcript_written=%s status=%s",
                        sid, result.summary_generated, result.transcript_written,
                        result.status.value,
                    )
                    yield pb.ServiceMessage(
                        conversation_finalized=pb.ConversationFinalized(
                            session_id=sid,
                            reason=pb.TRANSFER_SUCCESS,
                            summary_generated=result.summary_generated,
                            transcript_written=result.transcript_written,
                        )
                    )

                # The caller is gone: record the failure, but a spoken apology would only cost an
                # LLM call and a TTS pass.
                elif payload_case == "transfer_failed" and closing:
                    tf = msg.transfer_failed
                    log.info("Converse: held transfer_failed recorded without an apology, caller closed "
                             "session=%s", sid)
                    session.record_transfer_failed(tf.destination, tf.reason, tf.transfer_id)

                # Streams a spoken apology, same TTS sequencing as speech_ended.
                elif payload_case == "transfer_failed":
                    tf = msg.transfer_failed
                    duration_ms = (
                        int((time.monotonic() - transfer_started_at) * 1000)
                        if transfer_started_at is not None else -1
                    )
                    log.info("Converse: transfer_failed destination=%s reason=%s transfer_id=%s "
                             "duration_ms=%d session=%s",
                             tf.destination, tf.reason, tf.transfer_id, duration_ms, sid)

                    tf_tts_started_sent = False
                    # Kept for teardown, like the turn: its aclose() persists an apology cut short by a hangup.
                    apology = session.on_transfer_failed(tf.destination, tf.reason, tf.transfer_id)
                    async for response in apology:
                        if response.tts_payloads:
                            if not tf_tts_started_sent:
                                yield pb.ServiceMessage(tts_started=pb.TtsStarted(session_id=sid))
                                tf_tts_started_sent = True
                            if response.on_sent is not None:
                                response.on_sent()
                            for payload_bytes in response.tts_payloads:
                                tts_seq += 1
                                yield pb.ServiceMessage(
                                    tts_chunk=pb.TtsChunk(
                                        session_id=sid,
                                        sequence_num=tts_seq,
                                        codec=pb.AUDIO_CODEC_PCM_S16LE,
                                        sample_rate=16000,
                                        payload=payload_bytes,
                                        is_final=False,
                                    )
                                )

                    if tf_tts_started_sent:
                        tts_seq += 1
                        yield pb.ServiceMessage(
                            tts_chunk=pb.TtsChunk(
                                session_id=sid,
                                sequence_num=tts_seq,
                                codec=pb.AUDIO_CODEC_PCM_S16LE,
                                sample_rate=16000,
                                payload=b"",
                                is_final=True,
                            )
                        )

                else:
                    log.warning("Unknown payload_case=%s session=%s",
                                payload_case, sid)

        except RejectedCallHangup as exc:
            log.info("Converse: hanging up rejected call: %s", exc)
            yield pb.ServiceMessage(
                error=pb.ServiceError(session_id=sid, code="CALL_REJECTED", message=_REJECTED_MESSAGE, fatal=True)
            )
        except Exception:
            log.exception("Converse error session=%s", sid)
            yield pb.ServiceMessage(
                error=pb.ServiceError(
                    session_id=sid,
                    code="INTERNAL",
                    message=_INTERNAL_MESSAGE,
                    fatal=True,
                )
            )

        # ── 4. Teardown ────────────────────────────────────────────────────────
        finally:
            held.end_busy_turn()  # a backlog that never drained still reports its drop totals
            for pending_fut in (msg_fut, out_fut):
                if pending_fut is not None and not pending_fut.done():
                    pending_fut.cancel()
            if turn_stream is not None:
                await turn_stream.aclose()  # stops a turn still running behind the stream
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

            # A hangup abandons the turn at a yield; close it so the pipeline persists the turn before close().
            if turn is not None:
                await turn.aclose()
            if apology is not None:
                await apology.aclose()
            await session.close("stream_ended")
            # Drain the bus before stopping so SessionEnded and other close-path
            # events are delivered to subscribers.
            try:
                await asyncio.wait_for(bus.drain(), timeout=0.5)
            except asyncio.TimeoutError:
                log.debug("Converse: bus drain timed out session=%s", sid)
            await bus.stop()
            log.info("Converse: stream closed session=%s", sid)
