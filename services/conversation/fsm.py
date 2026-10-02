"""ConversationFSM: pure Python mirror of the C++ CallFSM (no I/O). Omits the
gateway-only WaitingForHangup state and adds Python-only RECOVERING."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


# ── States ────────────────────────────────────────────────────────────────────

class CallFsmState(Enum):
    IDLE          = "idle"
    CONNECTING    = "connecting"
    LISTENING     = "listening"
    RECOGNIZING   = "recognizing"
    THINKING      = "thinking"
    SYNTHESIZING  = "synthesizing"
    SPEAKING      = "speaking"
    BARGE_IN      = "barge_in"
    TRANSFERRING  = "transferring"
    # Python-only: LLM apology in flight after a failed transfer.
    RECOVERING    = "recovering"
    # After a successful transfer, while SessionFinalizer runs.
    FINALIZING    = "finalizing"
    CLOSING       = "closing"
    CLOSED        = "closed"


# ── Valid transitions (mirrors kValidTransitions in CallFSM.cpp) ─────────────

_VALID: frozenset[tuple[CallFsmState, CallFsmState]] = frozenset({
    # Happy path
    (CallFsmState.IDLE,          CallFsmState.CONNECTING  ),
    (CallFsmState.CONNECTING,    CallFsmState.LISTENING   ),
    (CallFsmState.LISTENING,     CallFsmState.RECOGNIZING ),
    (CallFsmState.RECOGNIZING,   CallFsmState.THINKING    ),
    (CallFsmState.THINKING,      CallFsmState.SYNTHESIZING),
    (CallFsmState.SYNTHESIZING,  CallFsmState.SPEAKING    ),
    (CallFsmState.SPEAKING,      CallFsmState.LISTENING   ),
    (CallFsmState.SPEAKING,      CallFsmState.BARGE_IN    ),
    (CallFsmState.BARGE_IN,      CallFsmState.LISTENING   ),
    # Timeout / error recovery → Listening
    (CallFsmState.RECOGNIZING,   CallFsmState.LISTENING   ),
    (CallFsmState.THINKING,      CallFsmState.LISTENING   ),
    (CallFsmState.SYNTHESIZING,  CallFsmState.LISTENING   ),
    # Transfer from any active state
    (CallFsmState.LISTENING,     CallFsmState.TRANSFERRING),
    (CallFsmState.RECOGNIZING,   CallFsmState.TRANSFERRING),
    (CallFsmState.THINKING,      CallFsmState.TRANSFERRING),
    (CallFsmState.SYNTHESIZING,  CallFsmState.TRANSFERRING),
    (CallFsmState.SPEAKING,      CallFsmState.TRANSFERRING),
    (CallFsmState.TRANSFERRING,  CallFsmState.CLOSING     ),  # transfer failed w/o recovery, or session_close
    (CallFsmState.TRANSFERRING,  CallFsmState.FINALIZING  ),
    (CallFsmState.FINALIZING,    CallFsmState.CLOSING     ),
    # Python-only: failed transfer -> apology -> resume conversation.
    (CallFsmState.TRANSFERRING,  CallFsmState.RECOVERING  ),
    (CallFsmState.RECOVERING,    CallFsmState.SPEAKING    ),
    # Teardown from any active state
    (CallFsmState.CONNECTING,    CallFsmState.CLOSING     ),
    (CallFsmState.LISTENING,     CallFsmState.CLOSING     ),
    (CallFsmState.RECOGNIZING,   CallFsmState.CLOSING     ),
    (CallFsmState.THINKING,      CallFsmState.CLOSING     ),
    (CallFsmState.SYNTHESIZING,  CallFsmState.CLOSING     ),
    (CallFsmState.SPEAKING,      CallFsmState.CLOSING     ),
    (CallFsmState.BARGE_IN,      CallFsmState.CLOSING     ),
    (CallFsmState.RECOVERING,    CallFsmState.CLOSING     ),
    (CallFsmState.CLOSING,       CallFsmState.CLOSED      ),
})


# ── Handlers (injected by SessionHandler) ────────────────────────────────────

@dataclass
class ConversationFsmHandlers:
    """
    Callback bundle injected at construction. Mirrors CallFsmHandlers in C++.
    All fields are optional; unset handlers are silently skipped.
    """
    # Fired on every transition — use to publish SessionStateChanged on EventBus
    on_state_changed:      Optional[Callable[[CallFsmState, CallFsmState, str, float], None]] = None

    # Semantic event callbacks
    on_speech_started:     Optional[Callable[[float], None]]              = None  # energy_db
    on_speech_ended:       Optional[Callable[[int, float], None]]         = None  # duration_ms, energy_db
    on_stt_final:          Optional[Callable[[str, float], None]]         = None  # text, confidence
    on_playback_finished:  Optional[Callable[[bool], None]]               = None  # interrupted
    on_transfer_requested: Optional[Callable[[str, str], None]]           = None  # queue_id, reason
    on_transfer_completed: Optional[Callable[[bool, str], None]]          = None  # success, transfer_id
    on_session_close:      Optional[Callable[[str], None]]                = None  # reason


# ── ConversationFSM ───────────────────────────────────────────────────────────

class ConversationFSM:
    """
    Session-level state machine for the Conversation Service.

    Usage::

        handlers = ConversationFsmHandlers(
            on_state_changed = lambda f, t, trigger, ms: bus.publish_nowait(
                SessionStateChanged(from_state=f.value, to_state=t.value, ...)
            ),
            on_stt_final = lambda text, conf: bus.publish_nowait(STTFinal(text=text, ...)),
        )
        fsm = ConversationFSM(session_id="abc", handlers=handlers)
        fsm.on_service_ready()
    """

    def __init__(
        self,
        session_id: str,
        handlers:   ConversationFsmHandlers,
        *,
        logger=None,   # optional standard-library-compatible logger
    ) -> None:
        self._session_id      = session_id
        self._handlers        = handlers
        self._logger          = logger
        self._state           = CallFsmState.IDLE
        self._state_entered   = time.monotonic()
        self._transition_count = 0

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def state(self) -> CallFsmState:
        return self._state

    @property
    def state_name(self) -> str:
        return self._state.value

    @property
    def is_terminal(self) -> bool:
        return self._state == CallFsmState.CLOSED

    @property
    def can_accept_audio(self) -> bool:
        return self._state in {
            CallFsmState.LISTENING,
            CallFsmState.RECOGNIZING,
            CallFsmState.SPEAKING,   # barge-in detection
            CallFsmState.BARGE_IN,
        }

    # ── Trigger methods ───────────────────────────────────────────────────────

    def on_session_start(self) -> None:
        self._transition(CallFsmState.CONNECTING, "session_start")

    def on_service_ready(self) -> None:
        if self._state != CallFsmState.CONNECTING:
            return
        self._transition(CallFsmState.LISTENING, "service_ready")

    def on_speech_started(self, energy_db: float = 0.0) -> None:
        if self._state != CallFsmState.LISTENING:
            return
        self._transition(CallFsmState.RECOGNIZING, "speech_started")
        if self._handlers.on_speech_started:
            self._handlers.on_speech_started(energy_db)

    def on_speech_ended(self, duration_ms: int = 0, energy_db: float = 0.0) -> None:
        # Stays in Recognizing — fires event for transport relay only.
        if self._state != CallFsmState.RECOGNIZING:
            return
        if self._handlers.on_speech_ended:
            self._handlers.on_speech_ended(duration_ms, energy_db)

    def on_stt_final(self, text: str, confidence: float = 1.0) -> None:
        if self._state != CallFsmState.RECOGNIZING:
            return
        self._transition(CallFsmState.THINKING, "stt_final")
        if self._handlers.on_stt_final:
            self._handlers.on_stt_final(text, confidence)

    def on_text_ready(self) -> None:
        if self._state != CallFsmState.THINKING:
            return
        self._transition(CallFsmState.SYNTHESIZING, "text_ready")

    def on_first_audio_chunk(self) -> None:
        if self._state != CallFsmState.SYNTHESIZING:
            return
        self._transition(CallFsmState.SPEAKING, "first_audio_chunk")

    def on_playback_finished(self, interrupted: bool = False) -> None:
        if self._state != CallFsmState.SPEAKING:
            return
        if interrupted:
            self._transition(CallFsmState.BARGE_IN, "playback_interrupted")
        else:
            self._transition(CallFsmState.LISTENING, "playback_finished")
            if self._handlers.on_playback_finished:
                self._handlers.on_playback_finished(False)

    def on_cancel_complete(self) -> None:
        if self._state != CallFsmState.BARGE_IN:
            return
        self._transition(CallFsmState.LISTENING, "cancel_complete")
        if self._handlers.on_playback_finished:
            self._handlers.on_playback_finished(True)  # interrupted=True

    def on_cancel(self) -> None:
        """Return FSM to LISTENING from any interruptible state after barge-in cancel.

        Handles all mid-pipeline states, not just SPEAKING:
          SPEAKING              → BARGE_IN → LISTENING
          THINKING/SYNTHESIZING → LISTENING (no playback to interrupt)
          RECOGNIZING           → LISTENING (no playback to interrupt)
        """
        if self._state == CallFsmState.SPEAKING:
            self._transition(CallFsmState.BARGE_IN, "barge_in")
            self._transition(CallFsmState.LISTENING, "cancel_complete")
            if self._handlers.on_playback_finished:
                self._handlers.on_playback_finished(True)
        elif self._state in {
            CallFsmState.RECOGNIZING,
            CallFsmState.THINKING,
            CallFsmState.SYNTHESIZING,
        }:
            self._transition(CallFsmState.LISTENING, "barge_in_cancel")

    def on_transfer_requested(self, queue_id: str, reason: str) -> None:
        if not self._is_active():
            return
        if self._handlers.on_transfer_requested:
            self._handlers.on_transfer_requested(queue_id, reason)
        self._transition(CallFsmState.TRANSFERRING, "transfer_requested")

    def on_transfer_completed(self, success: bool, transfer_id: str = "") -> None:
        if self._state != CallFsmState.TRANSFERRING:
            return
        if self._handlers.on_transfer_completed:
            self._handlers.on_transfer_completed(success, transfer_id)
        if success:
            self._transition(CallFsmState.FINALIZING, "transfer_completed")
        else:
            self._transition(CallFsmState.CLOSING, "transfer_failed")

    def on_session_finalized(self) -> None:
        """Post-transfer cleanup finished. Valid only from FINALIZING."""
        if self._state != CallFsmState.FINALIZING:
            return
        self._transition(CallFsmState.CLOSING, "session_finalized")

    def on_transfer_failed_event(self, reason: str = "") -> None:
        """TransferFailed: the conversation continues in RECOVERING. Valid only from TRANSFERRING."""
        if self._state != CallFsmState.TRANSFERRING:
            return
        self._transition(CallFsmState.RECOVERING, "transfer_failed")

    def on_recovery_response_ready(self) -> None:
        """Apology audio ready; like on_first_audio_chunk(). Valid only from RECOVERING."""
        if self._state != CallFsmState.RECOVERING:
            return
        self._transition(CallFsmState.SPEAKING, "recovery_response_ready")

    def on_session_close(self, reason: str = "caller_hangup") -> None:
        if self._state in {CallFsmState.CLOSING, CallFsmState.CLOSED}:
            return
        if self._handlers.on_session_close:
            self._handlers.on_session_close(reason)
        self._transition(CallFsmState.CLOSING, reason)

    def on_close_acknowledged(self) -> None:
        if self._state != CallFsmState.CLOSING:
            return
        self._transition(CallFsmState.CLOSED, "close_acknowledged")

    def on_timeout(self, state: CallFsmState) -> None:
        """Called by timer service when the active timer for a state fires."""
        if self._state != state:
            return  # stale timer — ignore

        recoverable = {
            CallFsmState.RECOGNIZING: "stt_timeout",
            CallFsmState.THINKING:    "llm_timeout",
            CallFsmState.SYNTHESIZING:"tts_timeout",
        }
        terminal = {
            CallFsmState.CONNECTING:  "connection_timeout",
            CallFsmState.LISTENING:   "no_speech_timeout",
            CallFsmState.SPEAKING:    "playback_timeout",
            CallFsmState.TRANSFERRING:"transfer_timeout",
        }

        if state in recoverable:
            if self._logger:
                self._logger.warning("session=%s %s — returning to Listening",
                                     self._session_id, recoverable[state])
            self._transition(CallFsmState.LISTENING, recoverable[state])
        elif state in terminal:
            self.on_session_close(terminal[state])
        elif state == CallFsmState.BARGE_IN:
            self.on_cancel_complete()
        elif state == CallFsmState.CLOSING:
            if self._logger:
                self._logger.warning("session=%s close_timeout — forcing Closed",
                                     self._session_id)
            self._transition(CallFsmState.CLOSED, "close_timeout_forced")

    # ── Core transition ───────────────────────────────────────────────────────

    def _transition(self, next_state: CallFsmState, trigger: str) -> None:
        prev = self._state

        if (prev, next_state) not in _VALID:
            if self._logger:
                self._logger.error(
                    "session=%s invalid_transition %s→%s trigger=%s",
                    self._session_id, prev.value, next_state.value, trigger,
                )
            return  # survive in production

        now          = time.monotonic()
        duration_ms  = (now - self._state_entered) * 1000.0

        self._state         = next_state
        self._state_entered = now
        self._transition_count += 1

        if self._logger:
            self._logger.debug(
                "session=%s fsm %s→%s trigger=%s prev_duration_ms=%.1f",
                self._session_id, prev.value, next_state.value, trigger, duration_ms,
            )

        if self._handlers.on_state_changed:
            self._handlers.on_state_changed(prev, next_state, trigger, duration_ms)

    def _is_active(self) -> bool:
        return self._state not in {
            CallFsmState.IDLE,
            CallFsmState.CLOSING,
            CallFsmState.CLOSED,
        }
