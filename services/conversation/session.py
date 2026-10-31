"""
ConversationSession — per-call state container for the Python service.

Owns:
  - ConversationFSM  (state machine, wired to EventBus)
  - EventBus         (observability: state change events)
  - IConversationHandler (audio processing + response generation)

Audio responses are returned directly from push_audio() so the servicer
can write them to the gRPC stream immediately without going through the bus.
The EventBus carries only observability/state events (SessionStateChanged, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncGenerator, Callable, Protocol

from .directives import TransferRequest
from .event_bus import (
    ConversationFinalized,
    EventBus,
    SessionEnded,
    SessionFinalizing,
    SessionStateChanged,
    SpeechEnded,
    SpeechStarted,
    TranscriptReady,
    TransferCompleted,
    TransferFailed,
    TransferInitiated,
)
from .fsm import CallFsmState, ConversationFSM, ConversationFsmHandlers
from .metrics import IMetrics, NullMetrics
from .session_finalizer import FinalizationResult

_SPEECH_ENDED_STATES = frozenset({CallFsmState.LISTENING, CallFsmState.RECOGNIZING})

# Per-utterance cap on buffered inbound audio: 60 s of 16 kHz S16LE mono.
MAX_UTTERANCE_BYTES = 60 * 16_000 * 2

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HandlerResponse + IConversationHandler
# ---------------------------------------------------------------------------

@dataclass
class HandlerResponse:
    """One result from an IConversationHandler turn.

    end_call_grace_period_ms: 0 = gateway default. response_text accompanies, never replaces, tts_payloads.
    filler: the tts_payloads are a "one moment" filler, not the reply, so they never count as the caller
    having heard an answer or a goodbye.
    end_call_without_audio: the max-duration cap only: EndCall goes out even when no audio played this turn.
    """
    stt_text:       str         = ""
    stt_confidence: float       = 0.0
    tts_payloads:   list[bytes] = field(default_factory=list)
    end_call:       bool        = False
    end_call_grace_period_ms: int = 0
    transfer_request: TransferRequest | None = None
    response_text:  str         = ""
    filler:         bool        = False
    # The servicer calls this once it sends the tts_payloads, so a handler records what the caller was sent.
    on_sent:        Callable[[], None] | None = None

    end_call_without_audio: bool = False


class IConversationHandler(Protocol):
    """
    Audio-processing backend for a conversation turn.

    - greeting        → synthesize the agent's opening line (called once on connect).
    - on_audio        → per-chunk processing; EchoHandler produces TTS here.
                        PipelineHandler returns empty and accumulates internally.
    - on_speech_ended → utterance complete; async-generator producing HandlerResponse
                        items (first: STT result, subsequent: TTS chunks).
    - on_cancel       → abort in-flight generation (barge-in).
    - on_session_end  → release any held resources.
    - on_transfer_failed → transfer failed; yields an apology like on_speech_ended.
    - on_transfer_cancelled → transfer dropped on barge-in; clear duplicate-transfer bookkeeping.
    - start_finalization → fire-and-forget speculative summary generation.
    - record_live_stage → update calls.live_stage for live monitoring.
    """

    async def greeting(self, session_id: str) -> list[bytes]: ...

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse: ...

    def on_speech_ended(
        self,
        session_id:  str,
        audio:       bytes,
        duration_ms: int,
        energy_db:   float,
    ) -> AsyncGenerator[HandlerResponse, None]: ...

    async def on_cancel(self, session_id: str, *, hanging_up: bool = False) -> None: ...
    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None: ...

    async def on_dtmf(self, session_id: str, digit: str) -> None: ...

    # Out-of-band responses with no inbound trigger (e.g. call-flow timeout); None if never used.
    # Implementers must declare it as a class attribute.
    out_responses: "asyncio.Queue[HandlerResponse] | None"

    def on_transfer_failed(
        self, session_id: str, destination: str, reason: str,
    ) -> AsyncGenerator[HandlerResponse, None]: ...

    def on_transfer_cancelled(self, session_id: str) -> None: ...

    def start_finalization(self, session_id: str) -> None: ...

    async def finalize_session(self, session_id: str, reason: str) -> FinalizationResult: ...

    def record_live_stage(self, session_id: str, stage: str) -> None: ...


# ---------------------------------------------------------------------------
# SessionContext
# ---------------------------------------------------------------------------

class RoutingStatus(Enum):
    # Not routed by the Gateway: webcall, provider bridges, tests.
    UNSPECIFIED = "unspecified"
    ROUTED      = "routed"
    ROUTED_LKG  = "routed_lkg"
    UNKNOWN     = "unknown"
    UNAVAILABLE = "unavailable"

    @property
    def rejects_call(self) -> bool:
        return self in (RoutingStatus.UNKNOWN, RoutingStatus.UNAVAILABLE)


@dataclass
class SessionContext:
    session_id:  str
    tenant_id:   str = ""
    trace_id:    str = ""
    call_id:     str = ""
    caller_did:  str = ""
    called_did:  str = ""
    direction:   str = ""
    script_id:   str = ""
    test_credential: str = field(default="", repr=False)
    routing_status: RoutingStatus = RoutingStatus.UNSPECIFIED


class AgentUnavailable(Exception):
    """A test credential was refused, or the agent it names did not resolve.
    Raised instead of falling back to the legacy agent."""


# ---------------------------------------------------------------------------
# ConversationSession
# ---------------------------------------------------------------------------

class ConversationSession:
    """
    One instance per active gRPC Converse stream.

    Lifecycle (called by servicer):
      session_ready()       — after SessionOpenRequest accepted
      push_audio(payload)   — each AudioChunk; returns TTS chunks to send
      cancel()              — CancelGeneration; returns True if ack needed
      close(reason)         — stream ends
    """

    def __init__(
        self,
        ctx:     SessionContext,
        bus:     EventBus,
        handler: IConversationHandler,
        metrics: IMetrics | None = None,
    ) -> None:
        self._ctx          = ctx
        self._bus          = bus
        self._handler      = handler
        self._metrics      = metrics if metrics is not None else NullMetrics()
        self._tts_seq      = 0
        self._audio_buffer = bytearray()   # accumulates inbound PCM per utterance
        self._cap_warned = False           # one over-cap warning per utterance
        # Last transfer outcome (TRANSFER_*), or None; persisted at close().
        self._transfer_outcome: str | None = None

        self._fsm = ConversationFSM(
            session_id=ctx.session_id,
            handlers=self._make_handlers(),
            logger=log,
        )

        # Metric only; the apology itself is streamed via on_transfer_failed().
        self._bus.subscribe(TransferFailed, self._on_transfer_failed_event)

    async def _on_transfer_failed_event(self, event: TransferFailed) -> None:
        self._metrics.increment("transfer_failures_total")

    # ── Accessors ──────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._ctx.session_id

    @property
    def fsm_state(self) -> CallFsmState:
        return self._fsm.state

    @property
    def is_terminal(self) -> bool:
        return self._fsm.is_terminal

    # ── Lifecycle called by servicer ───────────────────────────────────────────

    def session_ready(self) -> None:
        self._fsm.on_session_start()
        self._fsm.on_service_ready()

    async def greet(self) -> AsyncGenerator[HandlerResponse, None]:
        """Synthesize the opening greeting and yield it as a HandlerResponse."""
        payloads = await self._handler.greeting(self._ctx.session_id)
        ends_call = self._ctx.routing_status.rejects_call
        if payloads or ends_call:
            self._tts_seq += len(payloads)
            yield HandlerResponse(tts_payloads=payloads, end_call=ends_call)

    async def push_audio(self, payload: bytes, *, trace_id: str = "") -> HandlerResponse:
        """Accumulate inbound audio and call the handler's per-chunk hook."""
        if not self._fsm.can_accept_audio:
            return HandlerResponse()

        room = MAX_UTTERANCE_BYTES - len(self._audio_buffer)
        if len(payload) > room and not self._cap_warned:  # logged once per utterance
            self._cap_warned = True
            log.warning(
                "Utterance audio over cap, dropping excess: buffered=%d cap=%d session=%s",
                len(self._audio_buffer), MAX_UTTERANCE_BYTES, self._ctx.session_id,
            )
        self._audio_buffer.extend(payload[:max(room, 0)])
        response = await self._handler.on_audio(self._ctx.session_id, payload)

        if response.tts_payloads:
            self._tts_seq += len(response.tts_payloads)

        return response

    async def speech_ended(
        self, duration_ms: int, energy_db: float
    ) -> AsyncGenerator[HandlerResponse, None]:
        """Hand the buffered utterance to the handler and yield its responses."""
        # Any other state means a prior turn is still in flight.
        if self._fsm.state not in _SPEECH_ENDED_STATES:
            log.log(
                logging.INFO if self._audio_buffer else logging.DEBUG,
                "speech_ended: ignoring in state %s, discarding %d buffered bytes session=%s",
                self._fsm.state.value, len(self._audio_buffer), self._ctx.session_id,
            )
            # Audio buffered while busy (barge-in detection) must not leak into the next utterance.
            self._audio_buffer.clear()
            self._cap_warned = False
            return

        audio = bytes(self._audio_buffer)
        self._audio_buffer.clear()
        self._cap_warned = False

        self._fsm.on_speech_started(energy_db)
        self._fsm.on_speech_ended(duration_ms, energy_db)

        first_tts = True
        # aclosing: a hangup closes this generator, and the handler's turn must see it to persist.
        async with aclosing(self._handler.on_speech_ended(
            self._ctx.session_id, audio, duration_ms, energy_db
        )) as turn:
            async for response in turn:
                if response.tts_payloads:
                    self._tts_seq += len(response.tts_payloads)

                if response.stt_text:
                    self._fsm.on_stt_final(response.stt_text, response.stt_confidence)

                if response.tts_payloads and first_tts:
                    self._fsm.on_text_ready()
                    self._fsm.on_first_audio_chunk()
                    first_tts = False

                yield response

        # Turn didn't complete: return to LISTENING. SPEAKING too, since unsent
        # TTS means no playback_finished will ever arrive.
        if self._fsm.state in (
            CallFsmState.RECOGNIZING,
            CallFsmState.THINKING,
            CallFsmState.SYNTHESIZING,
            CallFsmState.SPEAKING,
        ):
            self._fsm.on_cancel()

    def on_playback_finished(self, interrupted: bool, played_bytes: int | None = None) -> None:
        """Called by the servicer when a PlaybackFinished message is received. played_bytes is the
        gateway's count of the cut reply's audio it sent; None from a gateway that does not report it."""
        self._fsm.on_playback_finished(interrupted=interrupted)
        if interrupted:
            on_interrupted = getattr(self._handler, "on_playback_interrupted", None)
            if played_bytes is not None and on_interrupted is not None:
                on_interrupted(self._ctx.session_id, played_bytes)
        else:
            on_completed = getattr(self._handler, "on_playback_completed", None)
            if on_completed is not None:
                on_completed(self._ctx.session_id)

    # ── Transfer notifications from the gateway ────────────────────────────────

    def on_transfer_initiated(self, transfer_type: str, destination: str, reason: str,
                              transfer_id: str = "") -> None:
        """Gateway issued the transfer; the session may be closed any time after this."""
        self._fsm.on_transfer_requested(destination, reason)
        self._metrics.increment("transfer_attempts_total")
        self._handler.record_live_stage(self._ctx.session_id, "waiting_for_human")
        self._bus.publish(TransferInitiated(
            session_id=self._ctx.session_id, transfer_type=transfer_type,
            destination=destination, reason=reason, transfer_id=transfer_id,
        ))
        # Speculatively start the summary now; discarded if the transfer fails.
        self._handler.start_finalization(self._ctx.session_id)

    async def on_transfer_completed(self, destination: str,
                                    transfer_id: str = "") -> FinalizationResult | None:
        """Destination bridged: finalize the session and return the result for ConversationFinalized.

        Returns None (and does nothing) when no transfer is pending: a stray or
        duplicate TransferCompleted must not finalize a live conversation.
        """
        if self._fsm.state != CallFsmState.TRANSFERRING:
            log.warning("Ignoring transfer_completed with no pending transfer state=%s session=%s",
                        self._fsm.state.name, self._ctx.session_id)
            return None
        self._transfer_outcome = "TRANSFER_SUCCESS"
        self._metrics.increment("transfer_success_total")
        self._fsm.on_transfer_completed(True, destination)
        self._handler.record_live_stage(self._ctx.session_id, "human_connected")
        self._bus.publish(TransferCompleted(session_id=self._ctx.session_id,
                                            destination=destination, transfer_id=transfer_id))
        self._bus.publish(SessionFinalizing(session_id=self._ctx.session_id))

        result = await self._handler.finalize_session(self._ctx.session_id, "transfer_completed")

        self._fsm.on_session_finalized()
        self._bus.publish(ConversationFinalized(
            session_id=self._ctx.session_id,
            reason="TRANSFER_SUCCESS",
            summary=result.summary,
            summary_generated=result.summary_generated,
            transcript_written=result.transcript_written,
        ))
        return result

    async def on_transfer_failed(
        self, destination: str, reason: str, transfer_id: str = "",
    ) -> AsyncGenerator[HandlerResponse, None]:
        """Transfer failed but the caller is usually still on the line: yield a spoken apology."""
        if self._fsm.state != CallFsmState.TRANSFERRING:
            log.warning("Ignoring transfer_failed with no pending transfer state=%s session=%s",
                        self._fsm.state.name, self._ctx.session_id)
            return
        start = time.monotonic()
        self._enter_transfer_failed(reason)

        any_audio = False
        first_audio = True
        # aclosing: a hangup closes this generator, and the handler's apology must see it to persist.
        async with aclosing(self._handler.on_transfer_failed(
            self._ctx.session_id, destination, reason,
        )) as apology:
            async for response in apology:
                if response.tts_payloads:
                    any_audio = True
                    if first_audio:
                        self._fsm.on_recovery_response_ready()
                        first_audio = False
                yield response

        if not any_audio:
            # No audio means no PlaybackFinished; advance the FSM synthetically.
            self._fsm.on_recovery_response_ready()
            self._fsm.on_playback_finished(interrupted=False)

        self._metrics.observe("transfer_recovery_latency_ms", (time.monotonic() - start) * 1000.0)
        if any_audio:
            self._metrics.increment("transfer_recovery_success_total")
        self._publish_transfer_failed(destination, reason, transfer_id)

    def record_transfer_failed(self, destination: str, reason: str, transfer_id: str = "") -> None:
        """The caller has gone: record the failed transfer (outcome, FSM, metrics, event) without
        the spoken apology, which would cost an LLM call and a TTS pass nobody hears."""
        start = time.monotonic()
        self._enter_transfer_failed(reason)
        self._fsm.on_recovery_response_ready()
        self._fsm.on_playback_finished(interrupted=False)
        self._metrics.observe("transfer_recovery_latency_ms", (time.monotonic() - start) * 1000.0)
        self._publish_transfer_failed(destination, reason, transfer_id)

    def _enter_transfer_failed(self, reason: str) -> None:
        self._transfer_outcome = (
            "TRANSFER_TIMEOUT" if reason == "transfer_timeout" else "TRANSFER_FAILED"
        )
        self._fsm.on_transfer_failed_event(reason)
        self._handler.record_live_stage(self._ctx.session_id, "ai")

    def _publish_transfer_failed(self, destination: str, reason: str, transfer_id: str) -> None:
        self._bus.publish(TransferFailed(
            session_id=self._ctx.session_id, destination=destination, reason=reason,
            transfer_id=transfer_id,
        ))

    def on_transfer_cancelled(self, transfer_id: str = "") -> None:
        """Pending transfer dropped on barge-in before dispatch; no FSM change."""
        self._transfer_outcome = "TRANSFER_CANCELLED"
        self._metrics.increment("transfer_cancelled_total")
        self._handler.record_live_stage(self._ctx.session_id, "ai")
        log.info("Transfer cancelled (barge-in before dispatch) transfer_id=%s session=%s",
                 transfer_id, self._ctx.session_id)
        self._handler.on_transfer_cancelled(self._ctx.session_id)

    async def cancel(self, hanging_up: bool = False) -> None:
        self._audio_buffer.clear()
        self._cap_warned = False
        await self._handler.on_cancel(self._ctx.session_id, hanging_up=hanging_up)
        self._fsm.on_cancel()  # handles SPEAKING/THINKING/SYNTHESIZING/RECOGNIZING → LISTENING

    async def push_dtmf(self, digit: str) -> None:
        """Forward a caller keypress; handler errors must not take the stream down."""
        try:
            await self._handler.on_dtmf(self._ctx.session_id, digit)
        except Exception:
            log.exception("push_dtmf: handler raised session=%s", self._ctx.session_id)

    @property
    def out_responses(self) -> "asyncio.Queue[HandlerResponse] | None":
        """The handler's out-of-band response queue, or None."""
        return getattr(self._handler, "out_responses", None)

    # Uninformative close reasons that a failed-transfer outcome may replace in persistence.
    _GENERIC_CLOSE_REASONS = frozenset(
        {"stream_ended", "close_timeout", "session_destroyed", "transport_error"}
    )

    async def close(self, reason: str = "caller_hangup") -> None:
        self._audio_buffer.clear()
        self._cap_warned = False
        effective = reason
        if self._transfer_outcome == "TRANSFER_SUCCESS":
            effective = "TRANSFER_SUCCESS"
        elif (self._transfer_outcome in ("TRANSFER_FAILED", "TRANSFER_TIMEOUT")
              and reason in self._GENERIC_CLOSE_REASONS):
            effective = self._transfer_outcome
        if not self._fsm.is_terminal:
            self._fsm.on_session_close(reason)
            self._fsm.on_close_acknowledged()
        await self._handler.on_session_end(
            self._ctx.session_id, effective, final_state=self._transfer_outcome,
        )
        self._bus.publish(SessionEnded(session_id=self._ctx.session_id, reason=effective))

    # ── Internal ───────────────────────────────────────────────────────────────

    def _make_handlers(self) -> ConversationFsmHandlers:
        sid = self._ctx.session_id

        def on_state_changed(
            from_s: CallFsmState, to_s: CallFsmState, trigger: str, ms: float
        ) -> None:
            self._bus.publish(SessionStateChanged(
                session_id=sid,
                from_state=from_s.value,
                to_state=to_s.value,
                trigger=trigger,
                prev_duration_ms=ms,
            ))

        def on_speech_started(energy_db: float) -> None:
            self._bus.publish(SpeechStarted(session_id=sid, energy_db=energy_db))

        def on_speech_ended(duration_ms: int, energy_db: float) -> None:
            self._bus.publish(SpeechEnded(
                session_id=sid,
                duration_ms=duration_ms,
                energy_db=energy_db,
            ))

        def on_stt_final(text: str, confidence: float) -> None:
            self._bus.publish(TranscriptReady(
                session_id=sid,
                text=text,
                confidence=confidence,
            ))

        return ConversationFsmHandlers(
            on_state_changed=on_state_changed,
            on_speech_started=on_speech_started,
            on_speech_ended=on_speech_ended,
            on_stt_final=on_stt_final,
        )
