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
    """
    Rich result from IConversationHandler.on_audio().

    stt_text / stt_confidence  — transcript produced for this audio segment;
                                  empty string means STT produced no output.
    tts_payloads               — zero or more raw PCM chunks to stream back.
    end_call                   — the agent decided this turn ends the call;
                                  the servicer sends EndCall once this turn's
                                  TTS is fully streamed (see pipeline.py).
    end_call_grace_period_ms   — ms the gateway should wait after playback for
                                  the caller to speak up before hanging up;
                                  0 = gateway uses its own configured default.
                                  Only meaningful when end_call is True.
    transfer_request           — set when this turn detected a [[TRANSFER]]
                                  directive or an escalation-threshold
                                  breach (see pipeline.py). The servicer
                                  publishes it as a TransferRequested event
                                  and sends TransferRequest to the gateway
                                  once this turn's audio finishes playing
                                  uninterrupted (see servicer.py).
    """
    stt_text:       str         = ""
    stt_confidence: float       = 0.0
    tts_payloads:   list[bytes] = field(default_factory=list)
    end_call:       bool        = False
    end_call_grace_period_ms: int = 0
    transfer_request: TransferRequest | None = None


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
    - on_transfer_failed → Phase 5C: a cold transfer failed; async-generator
                        producing an apology (TTS) so the conversation can
                        continue instead of ending — same yield shape as
                        on_speech_ended.
    - on_transfer_cancelled → Phase 6: a pending transfer was dropped before
                        dispatch (caller barge-in during the acknowledgment
                        — see ConversationSession.on_transfer_cancelled).
                        Lets the handler release any "a transfer is already
                        in flight for this session" bookkeeping it may be
                        keeping (see PipelineConversationHandler's
                        _transfer_requested / TransferDecisionEngine's
                        duplicate-suppression) so a caller who barges in
                        and then asks again isn't wrongly rejected as a
                        duplicate.
    - start_finalization → fire-and-forget: kicks off summary generation
                        speculatively, in parallel with the gateway's own
                        ring/answer/bridge sequence, instead of waiting
                        for finalize_session() to be called on
                        TransferCompleted (see session_finalizer.py's
                        start_summary_early()).
    - record_live_stage → Live Calls Monitoring's mid-call stage column
                        (database/schema.sql's calls.live_stage). Called
                        from the four transfer hooks below — the only
                        places transfer state is known mid-call — and
                        nothing else; a handler with no transcript
                        persistence (EchoHandler) is a no-op.
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
        # Last transfer attempt's outcome — TRANSFER_SUCCESS/FAILED/TIMEOUT/
        # CANCELLED, or None when no attempt happened. Persisted at close()
        # into calls.final_state (always) and calls.close_reason (only when
        # the transfer de facto ended the AI session — see close()).
        self._transfer_outcome: str | None = None

        self._fsm = ConversationFSM(
            session_id=ctx.session_id,
            handlers=self._make_handlers(),
            logger=log,
        )

        # Phase 5C requirement: "WorkflowEngine subscribes to TransferFailed"
        # — a real EventBus subscription (decoupled from the direct
        # servicer -> on_transfer_failed() call chain that actually streams
        # the apology's TTS back; the bus is fire-and-forget observability
        # only, see module docstring, so it can't be the thing that
        # produces a streamed gRPC response). This subscription exists
        # purely for the transfer_failures_total metric.
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
        """
        Accumulate inbound audio and call the handler's per-chunk hook.
        EchoHandler produces TTS here; PipelineHandler returns empty and waits
        for on_speech_ended() to fire when the utterance boundary is detected.
        """
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
        """
        Utterance boundary detected by the gateway's VAD.  Pass the accumulated
        audio buffer to the handler and yield its responses (STT result, then
        TTS chunks) as they are produced.  Clears the buffer afterwards.
        """
        # Guard: only process speech_ended from LISTENING.  Arriving in
        # RECOGNIZING/THINKING/etc. means a prior turn is still in flight; ignore.
        if self._fsm.state not in _SPEECH_ENDED_STATES:
            log.debug(
                "speech_ended: ignoring in state %s session=%s",
                self._fsm.state.value, self._ctx.session_id,
            )
            return

        audio = bytes(self._audio_buffer)
        self._audio_buffer.clear()

        # Drive FSM: LISTENING → RECOGNIZING.
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

        # If the handler exited without completing a turn (empty STT, barge-in
        # cancel, or pipeline exception), return the FSM to LISTENING from any
        # mid-turn state so the session stays responsive.
        # SPEAKING is included: if TTS was buffered by the servicer but never
        # sent to the gateway (e.g. barge-in cleared the buffer), the gateway
        # will never send playback_finished, leaving the FSM stuck in SPEAKING.
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

    # ── Phase 5B of AI-to-human transfer: gateway → service notifications ──
    # Purely reactive — drives ConversationFSM's TRANSFERRING state (its
    # first real production trigger; see fsm.py) and publishes the matching
    # EventBus event for observability. No LLM/prompt change, no fallback
    # speech, no workflow change results from any of these.

    def on_transfer_initiated(self, transfer_type: str, destination: str, reason: str,
                              transfer_id: str = "") -> None:
        """Called by the servicer when a TransferInitiated message arrives —
        the gateway has issued uuid_transfer and is waiting on FreeSWITCH to
        confirm the outcome. The AI session may be closed by the gateway at
        any point after this, so this is the one guaranteed chance to react."""
        self._fsm.on_transfer_requested(destination, reason)
        self._metrics.increment("transfer_attempts_total")
        self._handler.record_live_stage(self._ctx.session_id, "waiting_for_human")
        self._bus.publish(TransferInitiated(
            session_id=self._ctx.session_id, transfer_type=transfer_type,
            destination=destination, reason=reason, transfer_id=transfer_id,
        ))
        # Speculative: starts the summary LLM call now, in parallel with
        # whatever the gateway does next (uuid_transfer's near-instant
        # result for cold; ring/answer/bridge for warm), instead of only
        # starting it once TransferCompleted actually arrives. Discarded by
        # on_transfer_failed/on_transfer_cancelled below if this attempt
        # doesn't pan out — see session_finalizer.py's start_summary_early().
        self._handler.start_finalization(self._ctx.session_id)

    async def on_transfer_completed(self, destination: str,
                                    transfer_id: str = "") -> FinalizationResult:
        """
        Called by the servicer when a TransferCompleted message arrives —
        confirmed (a real CHANNEL_BRIDGE event, not just uuid_transfer's
        "+OK") that the destination answered and bridged.

        Phase 5D of AI-to-human transfer: drives the workflow Transferring
        -> Finalizing -> Closing (see fsm.py), running SessionFinalizer's
        post-call cleanup pipeline (via the handler's finalize_session() —
        see pipeline.py/session_finalizer.py) in between. Returns the full
        result so the servicer can build an accurate ConversationFinalized
        message (reason + whether the summary was really generated vs. a
        timeout fallback + whether it was persisted) — the gateway is
        waiting on that message before it tears its own side down (see
        CallFSM's Finalizing state).
        """
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
        """
        Called by the servicer when a TransferFailed message arrives.

        Phase 5C of AI-to-human transfer: unlike TransferCompleted (which
        ends the call — the transfer worked), a failure does NOT have to be
        terminal. The caller is often still on the line: the failure is the
        *destination's* (busy, rejected, no answer), not necessarily the
        caller's own channel. Delegates to the handler's own on_transfer_
        failed() to generate an apology through its usual LLM->TTS pipeline
        (see pipeline.py) and yields the results exactly like speech_ended()
        does, so the servicer streams them the same way.

        Drives the workflow transition Transferring -> Recovering -> Speaking
        (see fsm.py) as the apology's first audio becomes available; Speaking
        -> Listening is the *existing* on_playback_finished() transition,
        fired for real once the gateway acks the apology's own playback — no
        new mechanism needed for that last hop.
        """
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
            # No audio at all (the handler's own fallback synthesis also
            # failed) — there's nothing for the gateway to play, so no real
            # PlaybackFinished will ever arrive to close out Speaking.
            # Force the same Recovering -> Speaking -> Listening path
            # synthetically rather than leaving the FSM stuck.
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
        """A pending transfer was dropped before dispatch (caller barge-in
        during the acknowledgment — see servicer.py). No FSM change: the
        gateway never received the request, so no TransferInitiated ever
        arrives. Recorded for persistence (calls.final_state) only; a later
        attempt's real outcome overwrites it."""
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

    # close() reasons that carry no information beyond "the stream ended" —
    # a transfer outcome that de facto ended the AI session replaces these
    # in persistence. Deliberate reasons (goodbye_timeout, caller_hangup) are
    # never overridden: a failed transfer whose call genuinely continued and
    # later ended normally keeps its real close reason (the attempt is still
    # visible in calls.final_state).
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
