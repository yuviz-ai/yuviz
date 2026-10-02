"""Tests for ConversationFSM; mirrors the C++ call_fsm_test.cpp coverage."""

import pytest
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

from services.conversation.fsm import (
    CallFsmState,
    ConversationFSM,
    ConversationFsmHandlers,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

@dataclass
class Captured:
    transitions:         List[Tuple[CallFsmState, CallFsmState, str, float]] = field(default_factory=list)
    last_energy_db:      float = 0.0
    last_speech_end:     Optional[Tuple[int, float]] = None
    last_stt_text:       str = ""
    last_playback_int:   Optional[bool] = None
    last_transfer_queue: str = ""
    last_transfer_id:    Optional[Tuple[bool, str]] = None
    last_close_reason:   str = ""


def make_fsm(session_id: str = "test-session") -> Tuple[ConversationFSM, Captured]:
    cap = Captured()

    handlers = ConversationFsmHandlers(
        on_state_changed      = lambda f, t, trigger, ms: cap.transitions.append((f, t, trigger, ms)),
        on_speech_started     = lambda e: setattr(cap, "last_energy_db", e),
        on_speech_ended       = lambda d, e: setattr(cap, "last_speech_end", (d, e)),
        on_stt_final          = lambda txt, conf: setattr(cap, "last_stt_text", txt),
        on_playback_finished  = lambda i: setattr(cap, "last_playback_int", i),
        on_transfer_requested = lambda q, r: setattr(cap, "last_transfer_queue", q),
        on_transfer_completed = lambda ok, tid: setattr(cap, "last_transfer_id", (ok, tid)),
        on_session_close      = lambda r: setattr(cap, "last_close_reason", r),
    )
    fsm = ConversationFSM(session_id=session_id, handlers=handlers)
    return fsm, cap


def drive_to_listening(fsm: ConversationFSM) -> None:
    fsm.on_session_start()
    fsm.on_service_ready()


def drive_to_speaking(fsm: ConversationFSM) -> None:
    drive_to_listening(fsm)
    fsm.on_speech_started(0.3)
    fsm.on_stt_final("hello", 0.95)
    fsm.on_text_ready()
    fsm.on_first_audio_chunk()


# ── Initial state ──────────────────────────────────────────────────────────────

class TestInitialState:
    def test_starts_idle(self):
        fsm, _ = make_fsm()
        assert fsm.state == CallFsmState.IDLE

    def test_not_terminal_at_start(self):
        fsm, _ = make_fsm()
        assert not fsm.is_terminal

    def test_cannot_accept_audio_when_idle(self):
        fsm, _ = make_fsm()
        assert not fsm.can_accept_audio


# ── Happy path ─────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_session_start_moves_to_connecting(self):
        fsm, _ = make_fsm()
        fsm.on_session_start()
        assert fsm.state == CallFsmState.CONNECTING

    def test_service_ready_moves_to_listening(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        assert fsm.state == CallFsmState.LISTENING

    def test_speech_started_moves_to_recognizing(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.4)
        assert fsm.state == CallFsmState.RECOGNIZING
        assert cap.last_energy_db == pytest.approx(0.4)

    def test_stt_final_moves_to_thinking(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        fsm.on_stt_final("hello world", 0.97)
        assert fsm.state == CallFsmState.THINKING
        assert cap.last_stt_text == "hello world"

    def test_text_ready_moves_to_synthesizing(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        fsm.on_stt_final("hi", 0.9)
        fsm.on_text_ready()
        assert fsm.state == CallFsmState.SYNTHESIZING

    def test_first_audio_chunk_moves_to_speaking(self):
        fsm, _ = make_fsm()
        drive_to_speaking(fsm)
        assert fsm.state == CallFsmState.SPEAKING

    def test_playback_finished_clean_returns_to_listening(self):
        fsm, cap = make_fsm()
        drive_to_speaking(fsm)
        fsm.on_playback_finished(interrupted=False)
        assert fsm.state == CallFsmState.LISTENING
        assert cap.last_playback_int is False

    def test_full_turn_records_six_transitions(self):
        fsm, cap = make_fsm()
        drive_to_speaking(fsm)
        fsm.on_playback_finished(False)

        assert len(cap.transitions) == 7
        assert cap.transitions[0][:2] == (CallFsmState.IDLE, CallFsmState.CONNECTING)
        assert cap.transitions[6][:2] == (CallFsmState.SPEAKING, CallFsmState.LISTENING)

    def test_duration_reported_on_every_transition(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        assert len(cap.transitions) == 2
        for _, _, _, ms in cap.transitions:
            assert ms >= 0.0

    def test_multi_turn_stays_in_listening_loop(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        for _ in range(3):
            fsm.on_speech_started(0.3)
            fsm.on_stt_final("hi", 0.9)
            fsm.on_text_ready()
            fsm.on_first_audio_chunk()
            fsm.on_playback_finished(False)
            assert fsm.state == CallFsmState.LISTENING


# ── speech_ended (informational) ──────────────────────────────────────────────

class TestSpeechEnded:
    def test_speech_ended_stays_in_recognizing(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        before = len(cap.transitions)
        fsm.on_speech_started(0.3)
        fsm.on_speech_ended(400, 0.35)
        # No new state transition
        assert len(cap.transitions) == before + 1   # only speech_started transition
        assert fsm.state == CallFsmState.RECOGNIZING
        assert cap.last_speech_end == (400, 0.35)

    def test_speech_ended_ignored_outside_recognizing(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_ended(100, 0.1)      # wrong state
        assert cap.last_speech_end is None


# ── BargeIn ───────────────────────────────────────────────────────────────────

class TestBargeIn:
    def test_interrupted_playback_moves_to_barge_in(self):
        fsm, _ = make_fsm()
        drive_to_speaking(fsm)
        fsm.on_playback_finished(interrupted=True)
        assert fsm.state == CallFsmState.BARGE_IN

    def test_cancel_complete_returns_to_listening(self):
        fsm, cap = make_fsm()
        drive_to_speaking(fsm)
        fsm.on_playback_finished(interrupted=True)
        fsm.on_cancel_complete()
        assert fsm.state == CallFsmState.LISTENING
        assert cap.last_playback_int is True  # interrupted=True fired at cancel_complete

    def test_cancel_complete_ignored_outside_barge_in(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_cancel_complete()             # wrong state
        assert fsm.state == CallFsmState.LISTENING

    def test_can_accept_audio_in_barge_in(self):
        fsm, _ = make_fsm()
        drive_to_speaking(fsm)
        fsm.on_playback_finished(interrupted=True)
        assert fsm.can_accept_audio


# ── Transfer ──────────────────────────────────────────────────────────────────

class TestTransfer:
    def test_transfer_from_listening(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        assert fsm.state == CallFsmState.TRANSFERRING
        assert cap.last_transfer_queue == "billing"

    def test_transfer_completed_success_moves_to_finalizing(self):
        """Success waits in FINALIZING for SessionFinalizer instead of going straight to CLOSING."""
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_completed(True, "tx-123")
        assert fsm.state == CallFsmState.FINALIZING
        assert cap.last_transfer_id == (True, "tx-123")
        assert cap.transitions[-1][2] == "transfer_completed"

    def test_transfer_completed_failure_moves_to_closing(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_completed(False, "")
        assert fsm.state == CallFsmState.CLOSING
        assert cap.transitions[-1][2] == "transfer_failed"

    def test_transfer_from_thinking(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        fsm.on_stt_final("text", 0.9)
        assert fsm.state == CallFsmState.THINKING
        fsm.on_transfer_requested("support", "llm_decision")
        assert fsm.state == CallFsmState.TRANSFERRING

    def test_transfer_ignored_in_closing(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_session_close("caller_hangup")
        fsm.on_transfer_requested("billing", "oops")  # must be no-op
        assert fsm.state == CallFsmState.CLOSING


# ── Transfer recovery (Phase 5C) ────────────────────────────────────────────────

class TestTransferRecovery:
    def test_transfer_failed_event_moves_to_recovering(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        assert fsm.state == CallFsmState.TRANSFERRING

        fsm.on_transfer_failed_event("hangup_before_bridge")

        assert fsm.state == CallFsmState.RECOVERING
        assert cap.transitions[-1][2] == "transfer_failed"
        # Does NOT go through on_transfer_completed — different path/trigger.
        assert cap.last_transfer_id is None

    def test_recovery_response_ready_moves_to_speaking(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_failed_event("x")
        assert fsm.state == CallFsmState.RECOVERING

        fsm.on_recovery_response_ready()

        assert fsm.state == CallFsmState.SPEAKING

    def test_recovery_speaking_returns_to_listening_via_existing_playback_finished(self):
        """SPEAKING -> LISTENING after recovery reuses the existing on_playback_finished transition."""
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_failed_event("x")
        fsm.on_recovery_response_ready()
        assert fsm.state == CallFsmState.SPEAKING

        fsm.on_playback_finished(interrupted=False)

        assert fsm.state == CallFsmState.LISTENING
        assert cap.last_playback_int is False

    def test_full_recovery_cycle_from_speaking_state(self):
        """Full cycle: Transferring -> TransferFailed -> Recovering -> Speaking -> Listening, starting mid-turn."""
        fsm, _ = make_fsm()
        drive_to_speaking(fsm)
        assert fsm.state == CallFsmState.SPEAKING

        fsm.on_transfer_requested("billing", "escalation")
        assert fsm.state == CallFsmState.TRANSFERRING

        fsm.on_transfer_failed_event("esl_unreachable")
        assert fsm.state == CallFsmState.RECOVERING

        fsm.on_recovery_response_ready()
        assert fsm.state == CallFsmState.SPEAKING

        fsm.on_playback_finished(interrupted=False)
        assert fsm.state == CallFsmState.LISTENING

    def test_transfer_failed_event_ignored_outside_transferring(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_failed_event("x")  # not in TRANSFERRING — no-op
        assert fsm.state == CallFsmState.LISTENING

    def test_recovery_response_ready_ignored_outside_recovering(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_recovery_response_ready()  # not in RECOVERING — no-op
        assert fsm.state == CallFsmState.LISTENING

    def test_transfer_still_supports_terminal_failure_path(self):
        """The terminal on_transfer_completed(False, ...) -> CLOSING path (no recovery) still works."""
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_completed(False, "")
        assert fsm.state == CallFsmState.CLOSING
        assert cap.transitions[-1][2] == "transfer_failed"


# ── Session finalization (Phase 5D) ─────────────────────────────────────────────

class TestSessionFinalization:
    def test_session_finalized_moves_finalizing_to_closing(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_completed(True, "tx-123")
        assert fsm.state == CallFsmState.FINALIZING

        fsm.on_session_finalized()

        assert fsm.state == CallFsmState.CLOSING
        assert cap.transitions[-1][2] == "session_finalized"

    def test_session_finalized_ignored_outside_finalizing(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_session_finalized()  # not in FINALIZING — no-op
        assert fsm.state == CallFsmState.LISTENING

    def test_session_close_during_finalizing_moves_to_closing(self):
        """A caller/generic hangup mid-finalization must still be able to
        tear the session down — FINALIZING isn't exempt from teardown."""
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_completed(True, "tx-123")
        assert fsm.state == CallFsmState.FINALIZING

        fsm.on_session_close("caller_hangup")

        assert fsm.state == CallFsmState.CLOSING

    def test_full_finalization_cycle_to_closed(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_transfer_requested("billing", "escalation")
        fsm.on_transfer_completed(True, "tx-123")
        assert fsm.state == CallFsmState.FINALIZING

        fsm.on_session_finalized()
        assert fsm.state == CallFsmState.CLOSING

        fsm.on_close_acknowledged()
        assert fsm.state == CallFsmState.CLOSED


# ── Teardown ──────────────────────────────────────────────────────────────────

class TestTeardown:
    def test_close_from_listening_moves_to_closing(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_session_close("caller_hangup")
        assert fsm.state == CallFsmState.CLOSING
        assert cap.last_close_reason == "caller_hangup"

    def test_close_acknowledged_moves_to_closed(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_session_close("caller_hangup")
        fsm.on_close_acknowledged()
        assert fsm.state == CallFsmState.CLOSED
        assert fsm.is_terminal

    def test_close_from_thinking(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        fsm.on_stt_final("hi", 0.9)
        assert fsm.state == CallFsmState.THINKING
        fsm.on_session_close("caller_hangup")
        assert fsm.state == CallFsmState.CLOSING

    def test_double_close_is_idempotent(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_session_close("caller_hangup")
        before = len(cap.transitions)
        fsm.on_session_close("caller_hangup")   # second — must be no-op
        assert len(cap.transitions) == before

    def test_close_ack_ignored_outside_closing(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_close_acknowledged()              # wrong state
        assert fsm.state == CallFsmState.LISTENING

    def test_terminal_accepts_no_audio(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_session_close("bye")
        fsm.on_close_acknowledged()
        assert not fsm.can_accept_audio


# ── Timeout via on_timeout ────────────────────────────────────────────────────

class TestTimeouts:
    def test_stt_timeout_returns_to_listening(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        assert fsm.state == CallFsmState.RECOGNIZING
        fsm.on_timeout(CallFsmState.RECOGNIZING)
        assert fsm.state == CallFsmState.LISTENING

    def test_llm_timeout_returns_to_listening(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        fsm.on_stt_final("hi", 0.9)
        assert fsm.state == CallFsmState.THINKING
        fsm.on_timeout(CallFsmState.THINKING)
        assert fsm.state == CallFsmState.LISTENING

    def test_tts_timeout_returns_to_listening(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        fsm.on_stt_final("hi", 0.9)
        fsm.on_text_ready()
        assert fsm.state == CallFsmState.SYNTHESIZING
        fsm.on_timeout(CallFsmState.SYNTHESIZING)
        assert fsm.state == CallFsmState.LISTENING

    def test_no_speech_timeout_moves_to_closing(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_timeout(CallFsmState.LISTENING)   # NoSpeechTimeout
        assert fsm.state == CallFsmState.CLOSING

    def test_connection_timeout_moves_to_closing(self):
        fsm, _ = make_fsm()
        fsm.on_session_start()
        assert fsm.state == CallFsmState.CONNECTING
        fsm.on_timeout(CallFsmState.CONNECTING)
        assert fsm.state == CallFsmState.CLOSING

    def test_barge_in_window_timeout_cancels(self):
        fsm, cap = make_fsm()
        drive_to_speaking(fsm)
        fsm.on_playback_finished(interrupted=True)
        assert fsm.state == CallFsmState.BARGE_IN
        fsm.on_timeout(CallFsmState.BARGE_IN)    # BargeInWindow expired
        assert fsm.state == CallFsmState.LISTENING

    def test_stale_timeout_is_ignored(self):
        fsm, cap = make_fsm()
        drive_to_listening(fsm)
        fsm.on_speech_started(0.3)
        assert fsm.state == CallFsmState.RECOGNIZING
        before = len(cap.transitions)
        # NoSpeechTimeout was for Listening — stale now
        fsm.on_timeout(CallFsmState.LISTENING)
        assert fsm.state == CallFsmState.RECOGNIZING
        assert len(cap.transitions) == before

    def test_close_timeout_forces_closed(self):
        fsm, _ = make_fsm()
        drive_to_listening(fsm)
        fsm.on_session_close("bye")
        assert fsm.state == CallFsmState.CLOSING
        fsm.on_timeout(CallFsmState.CLOSING)
        assert fsm.state == CallFsmState.CLOSED


# ── Guard / invalid transitions ───────────────────────────────────────────────

class TestInvalidTransitions:
    def test_stt_final_from_idle_is_noop(self):
        fsm, cap = make_fsm()
        fsm.on_stt_final("hello", 0.9)
        assert fsm.state == CallFsmState.IDLE
        assert len(cap.transitions) == 0

    def test_service_ready_from_idle_is_noop(self):
        fsm, cap = make_fsm()
        fsm.on_service_ready()
        assert fsm.state == CallFsmState.IDLE
        assert len(cap.transitions) == 0

    def test_session_start_twice_is_noop(self):
        fsm, cap = make_fsm()
        fsm.on_session_start()
        before = len(cap.transitions)
        fsm.on_session_start()       # Connecting → Connecting invalid
        assert len(cap.transitions) == before


# ── can_accept_audio ──────────────────────────────────────────────────────────

class TestCanAcceptAudio:
    @pytest.mark.parametrize("state,expected", [
        (CallFsmState.IDLE,          False),
        (CallFsmState.CONNECTING,    False),
        (CallFsmState.LISTENING,     True),
        (CallFsmState.RECOGNIZING,   True),
        (CallFsmState.THINKING,      False),
        (CallFsmState.SYNTHESIZING,  False),
        (CallFsmState.SPEAKING,      True),
        (CallFsmState.BARGE_IN,      True),
        (CallFsmState.TRANSFERRING,  False),
        (CallFsmState.CLOSING,       False),
        (CallFsmState.CLOSED,        False),
    ])
    def test_can_accept_audio_per_state(self, state, expected):
        fsm, _ = make_fsm()
        fsm._state = state     # bypass transition for coverage
        assert fsm.can_accept_audio == expected


# ── State name ────────────────────────────────────────────────────────────────

class TestStateName:
    def test_state_name_matches_enum_value(self):
        fsm, _ = make_fsm()
        assert fsm.state_name == "idle"
        fsm.on_session_start()
        assert fsm.state_name == "connecting"

    def test_all_state_names_are_lowercase_strings(self):
        for s in CallFsmState:
            assert s.value == s.value.lower()


# ── Transition count ──────────────────────────────────────────────────────────

class TestTransitionCount:
    def test_transition_count_increments(self):
        fsm, cap = make_fsm()
        assert fsm._transition_count == 0
        drive_to_listening(fsm)
        assert fsm._transition_count == 2
