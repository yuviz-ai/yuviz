#include "session/CallFSM.h"

#include <array>
#include <utility>

namespace voiceai {

// The only place that defines legal state changes; any (from, to) pair not
// listed here is rejected at runtime and logged.
static constexpr std::array<std::pair<CallFsmState, CallFsmState>, 34> kValidTransitions{{
    // ── Happy path ─────────────────────────────────────────────────────────
    {CallFsmState::Idle,          CallFsmState::Connecting  },
    {CallFsmState::Connecting,    CallFsmState::Listening   },
    {CallFsmState::Listening,     CallFsmState::Recognizing },
    {CallFsmState::Recognizing,   CallFsmState::Thinking    },
    {CallFsmState::Thinking,      CallFsmState::Synthesizing},
    {CallFsmState::Synthesizing,  CallFsmState::Speaking    },
    {CallFsmState::Speaking,      CallFsmState::Listening   },  // PlaybackFinished
    {CallFsmState::Speaking,      CallFsmState::WaitingForHangup}, // PlaybackFinished, EndCall pending
    {CallFsmState::WaitingForHangup, CallFsmState::Recognizing},  // SpeechStarted — caller cancelled hangup
    {CallFsmState::Speaking,      CallFsmState::BargeIn     },  // SpeechStarted during playback
    {CallFsmState::Thinking,      CallFsmState::BargeIn     },  // SpeechStarted before audio arrives
    {CallFsmState::Synthesizing,  CallFsmState::BargeIn     },  // SpeechStarted before audio arrives
    {CallFsmState::BargeIn,       CallFsmState::Listening   },  // CancelComplete
    // ── Timeout / error recovery → back to Listening ───────────────────────
    {CallFsmState::Recognizing,   CallFsmState::Listening   },  // SttTimeout
    {CallFsmState::Thinking,      CallFsmState::Listening   },  // LlmTimeout
    {CallFsmState::Synthesizing,  CallFsmState::Listening   },  // TtsTimeout
    // ── Transfer from any active state ─────────────────────────────────────
    {CallFsmState::Listening,     CallFsmState::Transferring},
    {CallFsmState::Recognizing,   CallFsmState::Transferring},
    {CallFsmState::Thinking,      CallFsmState::Transferring},
    {CallFsmState::Synthesizing,  CallFsmState::Transferring},
    {CallFsmState::Speaking,      CallFsmState::Transferring},
    {CallFsmState::Transferring,  CallFsmState::Closing     },  // session_close mid-transfer
    {CallFsmState::Transferring,  CallFsmState::Thinking    },  // transfer_completed(success=false)
    // ── Finalization after a successful transfer ───────────────────────────
    {CallFsmState::Transferring,  CallFsmState::Finalizing  },  // transfer_completed(success=true)
    {CallFsmState::Finalizing,    CallFsmState::Closing     },  // ConversationFinalized, FinalizingTimeout, or session_close
    // ── Teardown from any active state ─────────────────────────────────────
    {CallFsmState::Connecting,    CallFsmState::Closing     },
    {CallFsmState::Listening,     CallFsmState::Closing     },
    {CallFsmState::Recognizing,   CallFsmState::Closing     },
    {CallFsmState::Thinking,      CallFsmState::Closing     },
    {CallFsmState::Synthesizing,  CallFsmState::Closing     },
    {CallFsmState::Speaking,      CallFsmState::Closing     },
    {CallFsmState::WaitingForHangup, CallFsmState::Closing  },  // GoodbyeTimeout, or caller hangup
    {CallFsmState::BargeIn,       CallFsmState::Closing     },
    {CallFsmState::Closing,       CallFsmState::Closed      },
}};

CallFSM::CallFSM(std::string         session_id,
                 CallFsmHandlers     handlers,
                 CallFsmTimerConfig  timer_cfg,
                 IMetrics&           metrics,
                 IClock&             clock,
                 Logger&             logger)
    : session_id_    (std::move(session_id))
    , handlers_      (std::move(handlers))
    , timer_cfg_     (timer_cfg)
    , metrics_       (metrics)
    , clock_         (clock)
    , logger_        (logger)
    , state_entered_at_(clock.now())
{}

void CallFSM::on_session_start() {
    transition(CallFsmState::Connecting, "session_start");
}

void CallFSM::on_service_ready() {
    if (state() != CallFsmState::Connecting) return;
    transition(CallFsmState::Listening, "service_ready");
}

void CallFSM::on_speech_started(float energy_db) {
    const auto s = state();
    if (s == CallFsmState::WaitingForHangup) {
        // Don't cancel the goodbye on a bare onset; require speech to outlast
        // GoodbyeConfirm (on_timer_fired) unless it ends first (a blip).
        if (active_timer_ != kNoTimer) {
            if (handlers_.cancel_timer) handlers_.cancel_timer(active_timer_);
            active_timer_ = kNoTimer;
        }
        if (handlers_.schedule_timer) {
            active_timer_ = handlers_.schedule_timer(
                FsmTimerType::GoodbyeConfirm, timer_cfg_.goodbye_confirm);
        }
        awaiting_goodbye_confirm_  = true;
        pending_goodbye_energy_db_ = energy_db;
        return;
    }
    if (s != CallFsmState::Listening) return;
    transition(CallFsmState::Recognizing, "speech_started");
    if (handlers_.on_speech_started) handlers_.on_speech_started(energy_db);
}

void CallFSM::notify_speech_ended(uint32_t duration_ms, float energy_db) {
    if (state() == CallFsmState::WaitingForHangup && awaiting_goodbye_confirm_) {
        // A blip: restore the full goodbye grace window.
        awaiting_goodbye_confirm_ = false;
        if (active_timer_ != kNoTimer) {
            if (handlers_.cancel_timer) handlers_.cancel_timer(active_timer_);
            active_timer_ = kNoTimer;
        }
        if (handlers_.schedule_timer) {
            active_timer_ = handlers_.schedule_timer(
                FsmTimerType::GoodbyeTimeout, timer_cfg_.goodbye_timeout);
        }
        logger_.info("session={} goodbye_confirm_blip duration_ms={} — speech ended before "
                     "confirm window, restoring grace period", session_id_, duration_ms);
        return;
    }
    if (state() != CallFsmState::Recognizing) return;

    // Swap MaxUtteranceTimeout for the tighter SttTimeout; no state change, so
    // on_exit/on_enter won't do it.
    if (active_timer_ != kNoTimer) {
        if (handlers_.cancel_timer) handlers_.cancel_timer(active_timer_);
        active_timer_ = kNoTimer;
    }
    if (handlers_.schedule_timer) {
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::SttTimeout, timer_cfg_.stt_timeout);
    }

    if (handlers_.on_speech_ended) handlers_.on_speech_ended(duration_ms, energy_db);
}

void CallFSM::on_stt_final(std::string text, float confidence) {
    if (state() != CallFsmState::Recognizing) return;
    if (text.empty()) {
        // Don't wait out SttTimeout on an empty result.
        transition(CallFsmState::Listening, "stt_empty");
        return;
    }
    transition(CallFsmState::Thinking, "stt_final");
    if (handlers_.on_stt_final) handlers_.on_stt_final(std::move(text), confidence);
}

void CallFSM::on_text_ready() {
    if (state() != CallFsmState::Thinking) return;
    transition(CallFsmState::Synthesizing, "text_ready");
}

void CallFSM::on_first_audio_chunk() {
    if (state() != CallFsmState::Synthesizing) return;
    transition(CallFsmState::Speaking, "first_audio_chunk");
}

void CallFSM::on_playback_finished(bool interrupted, bool end_call_pending,
                                   std::chrono::milliseconds goodbye_timeout_override) {
    if (state() != CallFsmState::Speaking) return;
    if (interrupted) {
        transition(CallFsmState::BargeIn, "playback_interrupted");
        return;
    }
    // on_playback_finished fires in both branches; Python has no WaitingForHangup.
    if (end_call_pending) {
        logger_.info("session={} goodbye played — entering grace period", session_id_);
        pending_goodbye_timeout_override_ = goodbye_timeout_override;
        transition(CallFsmState::WaitingForHangup, "goodbye_played");
    } else {
        transition(CallFsmState::Listening, "playback_finished");
    }
    if (handlers_.on_playback_finished) handlers_.on_playback_finished(false);
}

void CallFSM::on_cancel_complete() {
    do_cancel_complete_();
}

void CallFSM::on_early_barge_in() {
    const auto s = state();
    if (s != CallFsmState::Thinking && s != CallFsmState::Synthesizing) return;
    transition(CallFsmState::BargeIn, "early_barge_in");
}

void CallFSM::do_cancel_complete_() {
    if (state() != CallFsmState::BargeIn) return;
    transition(CallFsmState::Listening, "cancel_complete");
    if (handlers_.on_playback_finished) handlers_.on_playback_finished(true);
}

void CallFSM::on_transfer_requested(std::string queue_id, std::string reason) {
    if (!is_active()) return;
    if (handlers_.on_transfer_requested)
        handlers_.on_transfer_requested(queue_id, reason);
    transition(CallFsmState::Transferring, "transfer_requested");
}

void CallFSM::on_transfer_completed(bool success, std::string transfer_id) {
    do_transfer_completed_(success, std::move(transfer_id));
}

void CallFSM::do_transfer_completed_(bool success, std::string transfer_id) {
    if (state() != CallFsmState::Transferring) return;
    if (handlers_.on_transfer_completed)
        handlers_.on_transfer_completed(success, std::move(transfer_id));
    if (success) {
        transition(CallFsmState::Finalizing, "transfer_completed");
    } else {
        // The service speaks an apology as a normal turn, so enter Thinking for its
        // TTS transitions to apply; LlmTimeout covers a hung apology.
        transition(CallFsmState::Thinking, "transfer_failed");
    }
}

void CallFSM::on_conversation_finalized() {
    do_conversation_finalized_();
}

void CallFSM::do_conversation_finalized_() {
    if (state() != CallFsmState::Finalizing) return;
    if (handlers_.on_conversation_finalized)
        handlers_.on_conversation_finalized();
    transition(CallFsmState::Closing, "conversation_finalized");
}

void CallFSM::on_session_close(std::string reason) {
    do_session_close_(reason);
}

void CallFSM::do_session_close_(std::string_view reason) {
    if (state() == CallFsmState::Closing || state() == CallFsmState::Closed) return;
    if (handlers_.on_session_close) handlers_.on_session_close(std::string(reason));
    transition(CallFsmState::Closing, reason);
}

void CallFSM::on_close_acknowledged() {
    if (state() != CallFsmState::Closing) return;
    transition(CallFsmState::Closed, "close_acknowledged");
}

void CallFSM::on_timer_fired(FsmTimerType type) {
    active_timer_ = kNoTimer;  // timer consumed

    switch (type) {
    case FsmTimerType::ConnectionTimeout:
        if (state() == CallFsmState::Connecting)
            do_session_close_("connection_timeout");
        break;
    case FsmTimerType::NoSpeechTimeout:
        if (state() == CallFsmState::Listening)
            do_session_close_("no_speech_timeout");
        break;
    case FsmTimerType::MaxUtteranceTimeout:
        if (state() == CallFsmState::Recognizing) {
            logger_.warn("session={} max_utterance_timeout — returning to Listening",
                         session_id_);
            metrics_.increment("fsm.max_utterance_timeout");
            transition(CallFsmState::Listening, "max_utterance_timeout");
        }
        break;
    case FsmTimerType::SttTimeout:
        if (state() == CallFsmState::Recognizing) {
            logger_.warn("session={} stt_timeout — returning to Listening", session_id_);
            metrics_.increment("fsm.stt_timeout");
            transition(CallFsmState::Listening, "stt_timeout");
        }
        break;
    case FsmTimerType::LlmTimeout:
        if (state() == CallFsmState::Thinking) {
            logger_.warn("session={} llm_timeout — returning to Listening", session_id_);
            metrics_.increment("fsm.llm_timeout");
            transition(CallFsmState::Listening, "llm_timeout");
        }
        break;
    case FsmTimerType::TtsTimeout:
        if (state() == CallFsmState::Synthesizing) {
            logger_.warn("session={} tts_timeout — returning to Listening", session_id_);
            metrics_.increment("fsm.tts_timeout");
            transition(CallFsmState::Listening, "tts_timeout");
        }
        break;
    case FsmTimerType::PlaybackTimeout:
        if (state() == CallFsmState::Speaking)
            do_session_close_("playback_timeout");
        break;
    case FsmTimerType::GoodbyeTimeout:
        if (state() == CallFsmState::WaitingForHangup) {
            logger_.info("session={} goodbye_timeout — ending call", session_id_);
            metrics_.increment("fsm.goodbye_timeout");
            do_session_close_("goodbye_timeout");
        }
        break;
    case FsmTimerType::GoodbyeConfirm:
        if (state() == CallFsmState::WaitingForHangup && awaiting_goodbye_confirm_) {
            // Speech outlasted the confirm window: cancel the hangup.
            awaiting_goodbye_confirm_ = false;
            logger_.info("session={} goodbye_cancelled — caller spoke during grace period",
                         session_id_);
            transition(CallFsmState::Recognizing, "goodbye_cancelled");
            if (handlers_.on_speech_started) handlers_.on_speech_started(pending_goodbye_energy_db_);
        }
        break;
    case FsmTimerType::BargeInWindow:
        if (state() == CallFsmState::BargeIn)
            do_cancel_complete_();
        break;
    case FsmTimerType::TransferTimeout:
        if (state() == CallFsmState::Transferring)
            do_transfer_completed_(false, "");
        break;
    case FsmTimerType::FinalizingTimeout:
        if (state() == CallFsmState::Finalizing) {
            logger_.warn("session={} finalizing_timeout — proceeding to Closing without "
                         "ConversationFinalized ack", session_id_);
            metrics_.increment("fsm.finalizing_timeout");
            do_conversation_finalized_();
        }
        break;
    case FsmTimerType::CloseTimeout:
        if (state() == CallFsmState::Closing) {
            logger_.warn("session={} close_timeout — forcing Closed", session_id_);
            transition(CallFsmState::Closed, "close_timeout_forced");
        }
        break;
    case FsmTimerType::RtpInactivity:
        do_session_close_("rtp_inactivity");
        break;
    }
}

CallFsmState CallFSM::state() const noexcept {
    return state_.load(std::memory_order_acquire);
}

std::string_view CallFSM::state_name() const noexcept {
    return to_string(state());
}

bool CallFSM::is_terminal() const noexcept {
    return state() == CallFsmState::Closed;
}

bool CallFSM::can_accept_audio() const noexcept {
    const auto s = state();
    return s == CallFsmState::Listening
        || s == CallFsmState::Recognizing
        || s == CallFsmState::Thinking     // VAD must see audio for early barge-in
        || s == CallFsmState::Synthesizing // VAD must see audio for early barge-in
        || s == CallFsmState::Speaking     // barge-in detection during playback
        || s == CallFsmState::WaitingForHangup // VAD must see audio to cancel the pending hangup
        || s == CallFsmState::BargeIn;
}

void CallFSM::transition(CallFsmState next, std::string_view trigger) {
    const CallFsmState prev = state_.load(std::memory_order_acquire);

    if (!is_valid(prev, next)) {
        logger_.error("session={} invalid_transition {}→{} trigger={}",
                      session_id_,
                      to_string(prev), to_string(next), trigger);
        metrics_.increment("fsm.invalid_transition");
        return;  // survive in production — do NOT crash
    }

    on_exit(prev);

    const auto now_tp   = clock_.now();
    const auto duration = std::chrono::duration<double, std::milli>(
                              now_tp - state_entered_at_).count();

    state_.store(next, std::memory_order_release);
    state_entered_at_ = now_tp;

    on_enter(next);

    logger_.debug("session={} fsm {}→{} trigger={} prev_duration_ms={:.1f}",
                  session_id_,
                  to_string(prev), to_string(next), trigger, duration);
    metrics_.observe("fsm.state_duration_ms", duration);
    metrics_.increment("fsm.transition");

    if (handlers_.on_state_changed)
        handlers_.on_state_changed(prev, next, trigger, duration);
}

void CallFSM::on_exit(CallFsmState /*s*/) {
    if (active_timer_ != kNoTimer) {
        if (handlers_.cancel_timer) handlers_.cancel_timer(active_timer_);
        active_timer_ = kNoTimer;
    }
}

void CallFSM::on_enter(CallFsmState s) {
    if (!handlers_.schedule_timer) return;

    switch (s) {
    case CallFsmState::Connecting:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::ConnectionTimeout, timer_cfg_.connection_timeout);
        break;
    case CallFsmState::Listening:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::NoSpeechTimeout, timer_cfg_.no_speech_timeout);
        break;
    case CallFsmState::Recognizing:
        // notify_speech_ended() swaps this for SttTimeout.
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::MaxUtteranceTimeout, timer_cfg_.max_utterance_timeout);
        break;
    case CallFsmState::Thinking:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::LlmTimeout, timer_cfg_.llm_timeout);
        break;
    case CallFsmState::Synthesizing:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::TtsTimeout, timer_cfg_.tts_timeout);
        break;
    case CallFsmState::Speaking:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::PlaybackTimeout, timer_cfg_.playback_timeout);
        break;
    case CallFsmState::WaitingForHangup: {
        const auto timeout = pending_goodbye_timeout_override_.count() > 0
            ? pending_goodbye_timeout_override_
            : timer_cfg_.goodbye_timeout;
        active_timer_ = handlers_.schedule_timer(FsmTimerType::GoodbyeTimeout, timeout);
        pending_goodbye_timeout_override_ = {};   // consumed
        break;
    }
    case CallFsmState::BargeIn:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::BargeInWindow, timer_cfg_.barge_in_window);
        break;
    case CallFsmState::Transferring:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::TransferTimeout, timer_cfg_.transfer_timeout);
        break;
    case CallFsmState::Finalizing:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::FinalizingTimeout, timer_cfg_.finalizing_timeout);
        break;
    case CallFsmState::Closing:
        active_timer_ = handlers_.schedule_timer(
            FsmTimerType::CloseTimeout, timer_cfg_.close_timeout);
        break;
    case CallFsmState::Idle:
    case CallFsmState::Closed:
        break;  // terminal / initial — no timer
    }
}

bool CallFSM::is_valid(CallFsmState from, CallFsmState to) noexcept {
    for (const auto& [f, t] : kValidTransitions) {
        if (f == from && t == to) return true;
    }
    return false;
}

bool CallFSM::is_active() const noexcept {
    const auto s = state();
    return s != CallFsmState::Idle
        && s != CallFsmState::Closing
        && s != CallFsmState::Closed;
}

std::string_view to_string(CallFsmState s) noexcept {
    switch (s) {
    case CallFsmState::Idle:          return "Idle";
    case CallFsmState::Connecting:    return "Connecting";
    case CallFsmState::Listening:     return "Listening";
    case CallFsmState::Recognizing:   return "Recognizing";
    case CallFsmState::Thinking:      return "Thinking";
    case CallFsmState::Synthesizing:  return "Synthesizing";
    case CallFsmState::Speaking:      return "Speaking";
    case CallFsmState::WaitingForHangup: return "WaitingForHangup";
    case CallFsmState::BargeIn:       return "BargeIn";
    case CallFsmState::Transferring:  return "Transferring";
    case CallFsmState::Finalizing:    return "Finalizing";
    case CallFsmState::Closing:       return "Closing";
    case CallFsmState::Closed:        return "Closed";
    }
    return "Unknown";
}

} // namespace voiceai
