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
from dataclasses import dataclass, field
from typing import AsyncGenerator, Protocol

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

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HandlerResponse + IConversationHandler
# ---------------------------------------------------------------------------

@dataclass
class HandlerResponse:
    """One result from an IConversationHandler turn.

    end_call_grace_period_ms: 0 = gateway default. response_text accompanies, never replaces, tts_payloads.
    """
    stt_text:       str         = ""
    stt_confidence: float       = 0.0
    tts_payloads:   list[bytes] = field(default_factory=list)
    end_call:       bool        = False
    end_call_grace_period_ms: int = 0
    transfer_request: TransferRequest | None = None
    response_text:  str         = ""


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

    async def on_cancel(self, session_id: str) -> None: ...
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
        if payloads:
            self._tts_seq += len(payloads)
            yield HandlerResponse(tts_payloads=payloads)

    async def push_audio(self, payload: bytes, *, trace_id: str = "") -> HandlerResponse:
        """Accumulate inbound audio and call the handler's per-chunk hook."""
        if not self._fsm.can_accept_audio:
            return HandlerResponse()

        self._audio_buffer.extend(payload)
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
            log.debug(
                "speech_ended: ignoring in state %s session=%s",
                self._fsm.state.value, self._ctx.session_id,
            )
            return

        audio = bytes(self._audio_buffer)
        self._audio_buffer.clear()

        self._fsm.on_speech_started(energy_db)
        self._fsm.on_speech_ended(duration_ms, energy_db)

        first_tts = True
        async for response in self._handler.on_speech_ended(
            self._ctx.session_id, audio, duration_ms, energy_db
        ):
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

    def on_playback_finished(self, interrupted: bool) -> None:
        """Called by the servicer when a PlaybackFinished message is received."""
        self._fsm.on_playback_finished(interrupted=interrupted)

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
                                    transfer_id: str = "") -> FinalizationResult:
        """Destination bridged: finalize the session and return the result for ConversationFinalized."""
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
        start = time.monotonic()
        self._transfer_outcome = (
            "TRANSFER_TIMEOUT" if reason == "transfer_timeout" else "TRANSFER_FAILED"
        )
        self._fsm.on_transfer_failed_event(reason)
        self._handler.record_live_stage(self._ctx.session_id, "ai")

        any_audio = False
        first_audio = True
        async for response in self._handler.on_transfer_failed(
            self._ctx.session_id, destination, reason,
        ):
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

    async def cancel(self) -> None:
        self._audio_buffer.clear()
        await self._handler.on_cancel(self._ctx.session_id)
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
