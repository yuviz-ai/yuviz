#include <gtest/gtest.h>

#include "session/CallFSM.h"
#include "common/FakeClock.h"
#include "logging/Logger.h"
#include "metrics/IMetrics.h"

#include <algorithm>
#include <chrono>
#include <optional>
#include <string>
#include <vector>

namespace {

using namespace voiceai;
using namespace std::chrono_literals;

// ── Stub metrics ──────────────────────────────────────────────────────────────

class StubMetrics final : public IMetrics {
public:
    bool   initialize() override { return true; }
    bool   start()      override { return true; }
    void   stop()       override {}
    void   shutdown()   override {}

    void increment(const std::string& name, double /*value*/ = 1.0) override {
        increments.push_back(name);
    }
    void gauge(const std::string& /*name*/, double /*value*/) override {}
    void observe(const std::string& name, double value) override {
        observations.emplace_back(name, value);
    }

    std::vector<std::string>              increments;
    std::vector<std::pair<std::string, double>> observations;

    bool has_increment(const std::string& name) const {
        for (const auto& n : increments) if (n == name) return true;
        return false;
    }
};

// ── Recorded transitions ──────────────────────────────────────────────────────

struct Transition {
    CallFsmState from;
    CallFsmState to;
    std::string  trigger;
};

// ── Test fixture ──────────────────────────────────────────────────────────────

class CallFsmTest : public ::testing::Test {
protected:
    FakeClock    clock_;
    StubMetrics  metrics_;
    Logger       logger_ = Logger::make_null();

    std::vector<Transition>  transitions_;
    std::vector<FsmTimerType> scheduled_timers_;
    std::vector<std::chrono::milliseconds> scheduled_timer_durations_;  // parallel to scheduled_timers_
    std::vector<FsmTimerId>   cancelled_timers_;
    FsmTimerId next_timer_id_{1};

    // Semantic event capture
    float       last_energy_db_{0.0f};
    std::string last_stt_text_;
    bool        last_playback_interrupted_{false};
    std::string last_transfer_queue_;
    std::string last_close_reason_;
    bool        last_conversation_finalized_{false};

    CallFsmHandlers make_handlers() {
        CallFsmHandlers h;

        h.schedule_timer = [this](FsmTimerType t, std::chrono::milliseconds d) -> FsmTimerId {
            scheduled_timers_.push_back(t);
            scheduled_timer_durations_.push_back(d);
            return next_timer_id_++;
        };
        h.cancel_timer = [this](FsmTimerId id) {
            cancelled_timers_.push_back(id);
        };
        h.on_state_changed = [this](CallFsmState f, CallFsmState t,
                                    std::string_view trigger, double) {
            transitions_.push_back({f, t, std::string(trigger)});
        };
        h.on_speech_started = [this](float e) { last_energy_db_ = e; };
        h.on_stt_final = [this](std::string txt, float) { last_stt_text_ = std::move(txt); };
        h.on_playback_finished = [this](bool i) { last_playback_interrupted_ = i; };
        h.on_transfer_requested = [this](std::string q, std::string) {
            last_transfer_queue_ = std::move(q);
        };
        h.on_conversation_finalized = [this] { last_conversation_finalized_ = true; };
        h.on_session_close = [this](std::string r) { last_close_reason_ = std::move(r); };

        return h;
    }

    CallFSM make_fsm() {
        return CallFSM{"test-session", make_handlers(),
                       CallFsmTimerConfig{}, metrics_, clock_, logger_};
    }

    // Drives an FSM to Speaking via the happy path; by reference since CallFSM is non-movable.
    static void drive_to_speaking(CallFSM& fsm) {
        fsm.on_session_start();
        fsm.on_service_ready();
        fsm.on_speech_started(0.3f);
        fsm.on_stt_final("goodbye then", 0.9f);
        fsm.on_text_ready();
        fsm.on_first_audio_chunk();
    }

    bool has_timer(FsmTimerType t) const {
        for (auto st : scheduled_timers_) if (st == t) return true;
        return false;
    }

    // Duration passed to the most recent schedule_timer() call for `t`.
    std::optional<std::chrono::milliseconds> last_timer_duration(FsmTimerType t) const {
        for (size_t i = scheduled_timers_.size(); i-- > 0;) {
            if (scheduled_timers_[i] == t) return scheduled_timer_durations_[i];
        }
        return std::nullopt;
    }

    Transition last_transition() const { return transitions_.back(); }
};

// ── Happy path ────────────────────────────────────────────────────────────────

TEST_F(CallFsmTest, InitialStateIsIdle) {
    auto fsm = make_fsm();
    EXPECT_EQ(fsm.state(), CallFsmState::Idle);
    EXPECT_FALSE(fsm.is_terminal());
}

TEST_F(CallFsmTest, HappyPath_FullTurn) {
    auto fsm = make_fsm();

    fsm.on_session_start();
    EXPECT_EQ(fsm.state(), CallFsmState::Connecting);
    EXPECT_TRUE(has_timer(FsmTimerType::ConnectionTimeout));

    fsm.on_service_ready();
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(has_timer(FsmTimerType::NoSpeechTimeout));

    fsm.on_speech_started(0.4f);
    EXPECT_EQ(fsm.state(), CallFsmState::Recognizing);
    EXPECT_EQ(last_energy_db_, 0.4f);
    EXPECT_TRUE(has_timer(FsmTimerType::MaxUtteranceTimeout));

    fsm.notify_speech_ended(1200, 0.4f);
    EXPECT_EQ(fsm.state(), CallFsmState::Recognizing);   // unchanged, not a transition
    EXPECT_TRUE(has_timer(FsmTimerType::SttTimeout));

    fsm.on_stt_final("hello world", 0.97f);
    EXPECT_EQ(fsm.state(), CallFsmState::Thinking);
    EXPECT_EQ(last_stt_text_, "hello world");
    EXPECT_TRUE(has_timer(FsmTimerType::LlmTimeout));

    fsm.on_text_ready();
    EXPECT_EQ(fsm.state(), CallFsmState::Synthesizing);
    EXPECT_TRUE(has_timer(FsmTimerType::TtsTimeout));

    fsm.on_first_audio_chunk();
    EXPECT_EQ(fsm.state(), CallFsmState::Speaking);
    EXPECT_TRUE(has_timer(FsmTimerType::PlaybackTimeout));

    fsm.on_playback_finished(false);
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_FALSE(last_playback_interrupted_);
}

TEST_F(CallFsmTest, HappyPath_RecordsAllTransitions) {
    auto fsm = make_fsm();

    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("hi", 0.9f);
    fsm.on_text_ready();
    fsm.on_first_audio_chunk();
    fsm.on_playback_finished(false);

    // Idle→Connecting→Listening→Recognizing→Thinking→Synthesizing→Speaking→Listening = 7
    ASSERT_EQ(transitions_.size(), 7u);
    EXPECT_EQ(transitions_[0].from, CallFsmState::Idle);
    EXPECT_EQ(transitions_[0].to,   CallFsmState::Connecting);
    EXPECT_EQ(transitions_[6].from, CallFsmState::Speaking);
    EXPECT_EQ(transitions_[6].to,   CallFsmState::Listening);
}

// ── WaitingForHangup (agent-initiated end-of-call) ─────────────────────────────

TEST_F(CallFsmTest, EndCallPending_EntersWaitingForHangup) {
    auto fsm = make_fsm();
    drive_to_speaking(fsm);

    fsm.on_playback_finished(/*interrupted=*/false, /*end_call_pending=*/true);
    EXPECT_EQ(fsm.state(), CallFsmState::WaitingForHangup);
    EXPECT_TRUE(has_timer(FsmTimerType::GoodbyeTimeout));
    // on_playback_finished still fires with interrupted=false on this branch.
    EXPECT_FALSE(last_playback_interrupted_);
}

TEST_F(CallFsmTest, WaitingForHangup_SpeechStartedAwaitsConfirmBeforeCancelling) {
    // A bare VAD onset doesn't cancel the goodbye until the confirm timer fires.
    auto fsm = make_fsm();
    drive_to_speaking(fsm);
    fsm.on_playback_finished(false, true);
    ASSERT_EQ(fsm.state(), CallFsmState::WaitingForHangup);

    fsm.on_speech_started(0.5f);
    EXPECT_EQ(fsm.state(), CallFsmState::WaitingForHangup);
    EXPECT_EQ(scheduled_timers_.back(), FsmTimerType::GoodbyeConfirm);
    // on_speech_started hasn't fired yet: last_energy_db_ is still the earlier value.
    EXPECT_NE(last_energy_db_, 0.5f);
}

TEST_F(CallFsmTest, WaitingForHangup_GoodbyeConfirmFiring_CancelsHangup) {
    auto fsm = make_fsm();
    drive_to_speaking(fsm);
    fsm.on_playback_finished(false, true);
    fsm.on_speech_started(0.5f);
    ASSERT_EQ(fsm.state(), CallFsmState::WaitingForHangup);

    fsm.on_timer_fired(FsmTimerType::GoodbyeConfirm);
    // Recognizing, not Listening: VAD is edge-triggered, and preroll_ only flushes on Recognizing.
    EXPECT_EQ(fsm.state(), CallFsmState::Recognizing);
    EXPECT_EQ(transitions_.back().trigger, "goodbye_cancelled");
    EXPECT_EQ(last_energy_db_, 0.5f);  // handlers_.on_speech_started fired now
}

TEST_F(CallFsmTest, WaitingForHangup_BlipEndsBeforeConfirm_RestoresGoodbyeTimeout) {
    // Speech ending before confirm means a blip: keep the hangup, re-arm the grace window.
    auto fsm = make_fsm();
    drive_to_speaking(fsm);
    fsm.on_playback_finished(false, true);
    fsm.on_speech_started(0.5f);
    ASSERT_EQ(fsm.state(), CallFsmState::WaitingForHangup);

    fsm.notify_speech_ended(80, 0.5f);
    EXPECT_EQ(fsm.state(), CallFsmState::WaitingForHangup);  // never cancelled
    EXPECT_EQ(scheduled_timers_.back(), FsmTimerType::GoodbyeTimeout);
    EXPECT_NE(last_energy_db_, 0.5f);  // handlers_.on_speech_started never fired

    // Stale GoodbyeConfirm is a no-op now.
    fsm.on_timer_fired(FsmTimerType::GoodbyeConfirm);
    EXPECT_EQ(fsm.state(), CallFsmState::WaitingForHangup);
}

TEST_F(CallFsmTest, WaitingForHangup_TimeoutClosesSession) {
    auto fsm = make_fsm();
    drive_to_speaking(fsm);
    fsm.on_playback_finished(false, true);
    ASSERT_EQ(fsm.state(), CallFsmState::WaitingForHangup);

    fsm.on_timer_fired(FsmTimerType::GoodbyeTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
    EXPECT_EQ(last_close_reason_, "goodbye_timeout");
    EXPECT_TRUE(metrics_.has_increment("fsm.goodbye_timeout"));
}

TEST_F(CallFsmTest, EndCallPending_IgnoredWhenInterrupted) {
    // A caller barge-in during the goodbye takes priority: interrupted=true
    // always goes to BargeIn, regardless of end_call_pending.
    auto fsm = make_fsm();
    drive_to_speaking(fsm);

    fsm.on_playback_finished(/*interrupted=*/true, /*end_call_pending=*/true);
    EXPECT_EQ(fsm.state(), CallFsmState::BargeIn);
}

TEST_F(CallFsmTest, WaitingForHangup_CanAcceptAudio) {
    // Caller audio must be accepted during the grace window.
    auto fsm = make_fsm();
    drive_to_speaking(fsm);
    fsm.on_playback_finished(false, true);
    ASSERT_EQ(fsm.state(), CallFsmState::WaitingForHangup);
    EXPECT_TRUE(fsm.can_accept_audio());
}

TEST_F(CallFsmTest, WaitingForHangup_UsesGoodbyeTimeoutOverride) {
    // Per-agent EndCall.grace_period_ms overrides the armed timer's duration.
    auto fsm = make_fsm();
    drive_to_speaking(fsm);

    fsm.on_playback_finished(false, true, std::chrono::milliseconds{750});
    ASSERT_EQ(fsm.state(), CallFsmState::WaitingForHangup);

    const auto d = last_timer_duration(FsmTimerType::GoodbyeTimeout);
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(*d, std::chrono::milliseconds{750});
}

TEST_F(CallFsmTest, WaitingForHangup_ZeroOverrideFallsBackToConfigDefault) {
    // grace_period_ms == 0 (proto3 default) falls back to goodbye_timeout, not an instant hangup.
    auto fsm = make_fsm();
    drive_to_speaking(fsm);

    fsm.on_playback_finished(false, true, std::chrono::milliseconds{0});
    ASSERT_EQ(fsm.state(), CallFsmState::WaitingForHangup);

    const auto d = last_timer_duration(FsmTimerType::GoodbyeTimeout);
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(*d, CallFsmTimerConfig{}.goodbye_timeout);
}

// ── BargeIn ───────────────────────────────────────────────────────────────────

// Barge-in path: interrupted playback (on_playback_finished(interrupted=true))
// transitions Speaking→BargeIn.  CancelComplete (or BargeInWindow expiry) then
// returns to Listening.
TEST_F(CallFsmTest, BargeIn_SpeechDuringPlayback) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("text", 0.9f);
    fsm.on_text_ready();
    fsm.on_first_audio_chunk();
    ASSERT_EQ(fsm.state(), CallFsmState::Speaking);

    fsm.on_playback_finished(true);   // interrupted
    EXPECT_EQ(fsm.state(), CallFsmState::BargeIn);
    EXPECT_TRUE(has_timer(FsmTimerType::BargeInWindow));
}

TEST_F(CallFsmTest, BargeIn_CancelComplete_ReturnsToListening) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("text", 0.9f);
    fsm.on_text_ready();
    fsm.on_first_audio_chunk();
    fsm.on_playback_finished(true);
    ASSERT_EQ(fsm.state(), CallFsmState::BargeIn);

    fsm.on_cancel_complete();
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(last_playback_interrupted_);  // fired with interrupted=true
}

// ── Transfer ──────────────────────────────────────────────────────────────────

TEST_F(CallFsmTest, Transfer_FromListening) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    ASSERT_EQ(fsm.state(), CallFsmState::Listening);

    fsm.on_transfer_requested("billing", "customer_requested");
    EXPECT_EQ(fsm.state(), CallFsmState::Transferring);
    EXPECT_EQ(last_transfer_queue_, "billing");
    EXPECT_TRUE(has_timer(FsmTimerType::TransferTimeout));
}

// TransferRequest arrives mid-turn, so Speaking (not Listening) is the realistic pre-state.
TEST_F(CallFsmTest, Transfer_FromSpeaking) {
    auto fsm = make_fsm();
    drive_to_speaking(fsm);
    ASSERT_EQ(fsm.state(), CallFsmState::Speaking);

    fsm.on_transfer_requested("+15551234567", "escalation_threshold_exceeded");
    EXPECT_EQ(fsm.state(), CallFsmState::Transferring);
    EXPECT_EQ(last_transfer_queue_, "+15551234567");
    EXPECT_EQ(last_transition().trigger, "transfer_requested");
    EXPECT_TRUE(has_timer(FsmTimerType::TransferTimeout));
}

TEST_F(CallFsmTest, Transfer_CompletedSuccess_MovesToFinalizing) {
    // Success waits in Finalizing for ConversationFinalized before Closing.
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_transfer_requested("billing", "escalation");
    ASSERT_EQ(fsm.state(), CallFsmState::Transferring);

    fsm.on_transfer_completed(true, "tx-123");
    EXPECT_EQ(fsm.state(), CallFsmState::Finalizing);
    EXPECT_EQ(last_transition().trigger, "transfer_completed");
    EXPECT_TRUE(has_timer(FsmTimerType::FinalizingTimeout));
}

TEST_F(CallFsmTest, Transfer_CompletedFailure_MovesToThinkingNotClosing) {
    // Failure isn't terminal: land in Thinking so the apology turn drives Synthesizing→Speaking.
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_transfer_requested("billing", "escalation");
    fsm.on_transfer_completed(false, "");
    EXPECT_EQ(fsm.state(), CallFsmState::Thinking);
    EXPECT_EQ(last_transition().trigger, "transfer_failed");
    // LlmTimeout covers a hung apology.
    EXPECT_TRUE(has_timer(FsmTimerType::LlmTimeout));
}

TEST_F(CallFsmTest, Transfer_Timeout_MovesToThinking) {
    // TransferTimeout resolves like an explicit failure.
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_transfer_requested("billing", "escalation");
    ASSERT_EQ(fsm.state(), CallFsmState::Transferring);

    fsm.on_timer_fired(FsmTimerType::TransferTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Thinking);
    EXPECT_EQ(last_transition().trigger, "transfer_failed");
}

TEST_F(CallFsmTest, Transfer_SessionCloseDuringTransferring_MovesToClosing) {
    // Hangup mid-transfer still tears down directly (unlike a failed outcome).
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_transfer_requested("billing", "escalation");
    ASSERT_EQ(fsm.state(), CallFsmState::Transferring);

    fsm.on_session_close("caller_hangup");
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
}

// ── Finalizing (Phase 5D) ───────────────────────────────────────────────────

TEST_F(CallFsmTest, Transfer_ConversationFinalized_MovesToClosing) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_transfer_requested("billing", "escalation");
    fsm.on_transfer_completed(true, "tx-123");
    ASSERT_EQ(fsm.state(), CallFsmState::Finalizing);

    fsm.on_conversation_finalized();
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
    EXPECT_EQ(last_transition().trigger, "conversation_finalized");
    EXPECT_TRUE(last_conversation_finalized_);
}

TEST_F(CallFsmTest, Transfer_ConversationFinalized_IgnoredOutsideFinalizing) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    ASSERT_EQ(fsm.state(), CallFsmState::Listening);

    fsm.on_conversation_finalized();  // not in Finalizing — no-op
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
}

TEST_F(CallFsmTest, Transfer_FinalizingTimeout_ForcesClosing) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_transfer_requested("billing", "escalation");
    fsm.on_transfer_completed(true, "tx-123");
    ASSERT_EQ(fsm.state(), CallFsmState::Finalizing);

    fsm.on_timer_fired(FsmTimerType::FinalizingTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
    EXPECT_EQ(last_transition().trigger, "conversation_finalized");
    EXPECT_TRUE(last_conversation_finalized_);
}

TEST_F(CallFsmTest, Transfer_SessionCloseDuringFinalizing_MovesToClosing) {
    // Hangup mid-finalization still tears down.
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_transfer_requested("billing", "escalation");
    fsm.on_transfer_completed(true, "tx-123");
    ASSERT_EQ(fsm.state(), CallFsmState::Finalizing);

    fsm.on_session_close("caller_hangup");
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
}

// ── Teardown ──────────────────────────────────────────────────────────────────

TEST_F(CallFsmTest, SessionClose_FromListening) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();

    fsm.on_session_close("caller_hangup");
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
    EXPECT_EQ(last_close_reason_, "caller_hangup");

    fsm.on_close_acknowledged();
    EXPECT_EQ(fsm.state(), CallFsmState::Closed);
    EXPECT_TRUE(fsm.is_terminal());
}

TEST_F(CallFsmTest, SessionClose_FromThinking) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("hi", 0.9f);
    ASSERT_EQ(fsm.state(), CallFsmState::Thinking);

    fsm.on_session_close("caller_hangup");
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
}

TEST_F(CallFsmTest, DoubleClose_IsIdempotent) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();

    fsm.on_session_close("caller_hangup");
    const auto before = transitions_.size();
    fsm.on_session_close("caller_hangup");  // second call — must be no-op
    EXPECT_EQ(transitions_.size(), before);
}

// ── Timer expirations ─────────────────────────────────────────────────────────

TEST_F(CallFsmTest, MaxUtteranceTimeout_ReturnsToListening) {
    // Fires while the caller is still talking: safety net, not the STT budget.
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    ASSERT_EQ(fsm.state(), CallFsmState::Recognizing);

    fsm.on_timer_fired(FsmTimerType::MaxUtteranceTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(metrics_.has_increment("fsm.max_utterance_timeout"));
}

TEST_F(CallFsmTest, SttTimeout_ReturnsToListening) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    ASSERT_EQ(fsm.state(), CallFsmState::Recognizing);

    fsm.on_timer_fired(FsmTimerType::SttTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(metrics_.has_increment("fsm.stt_timeout"));
}

TEST_F(CallFsmTest, NotifySpeechEnded_SwapsMaxUtteranceTimeoutForSttTimeout) {
    // After speech_ended only SttTimeout is live; MaxUtteranceTimeout was swapped out.
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    ASSERT_EQ(fsm.state(), CallFsmState::Recognizing);
    const auto max_utterance_timer_id = next_timer_id_ - 1;

    fsm.notify_speech_ended(9'000, 0.3f);   // e.g. a 9s-long utterance
    EXPECT_EQ(fsm.state(), CallFsmState::Recognizing);
    EXPECT_TRUE(std::find(cancelled_timers_.begin(), cancelled_timers_.end(),
                          max_utterance_timer_id) != cancelled_timers_.end());

    // The timer service drops cancelled timers; at FSM level, verify STT gets its own budget.
    fsm.on_timer_fired(FsmTimerType::SttTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(metrics_.has_increment("fsm.stt_timeout"));
}

TEST_F(CallFsmTest, LlmTimeout_ReturnsToListening) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("hi", 0.9f);
    ASSERT_EQ(fsm.state(), CallFsmState::Thinking);

    fsm.on_timer_fired(FsmTimerType::LlmTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(metrics_.has_increment("fsm.llm_timeout"));
}

TEST_F(CallFsmTest, TtsTimeout_ReturnsToListening) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("hi", 0.9f);
    fsm.on_text_ready();
    ASSERT_EQ(fsm.state(), CallFsmState::Synthesizing);

    fsm.on_timer_fired(FsmTimerType::TtsTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(metrics_.has_increment("fsm.tts_timeout"));
}

TEST_F(CallFsmTest, NoSpeechTimeout_MovesToClosing) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    ASSERT_EQ(fsm.state(), CallFsmState::Listening);

    fsm.on_timer_fired(FsmTimerType::NoSpeechTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
}

TEST_F(CallFsmTest, StaleTimer_IsIgnored) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    ASSERT_EQ(fsm.state(), CallFsmState::Recognizing);

    // NoSpeechTimeout was from Listening — stale in Recognizing
    fsm.on_timer_fired(FsmTimerType::NoSpeechTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Recognizing);   // unchanged
}

TEST_F(CallFsmTest, PlaybackTimeout_MovesToClosing) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("hi", 0.9f);
    fsm.on_text_ready();
    fsm.on_first_audio_chunk();
    ASSERT_EQ(fsm.state(), CallFsmState::Speaking);

    fsm.on_timer_fired(FsmTimerType::PlaybackTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
}

TEST_F(CallFsmTest, BargeInWindow_ReturnsToListening) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_speech_started(0.3f);
    fsm.on_stt_final("hi", 0.9f);
    fsm.on_text_ready();
    fsm.on_first_audio_chunk();
    fsm.on_playback_finished(true);
    ASSERT_EQ(fsm.state(), CallFsmState::BargeIn);

    fsm.on_timer_fired(FsmTimerType::BargeInWindow);
    EXPECT_EQ(fsm.state(), CallFsmState::Listening);
    EXPECT_TRUE(last_playback_interrupted_);
}

TEST_F(CallFsmTest, CloseTimeout_ForcesClosed) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    fsm.on_session_close("test");
    ASSERT_EQ(fsm.state(), CallFsmState::Closing);

    fsm.on_timer_fired(FsmTimerType::CloseTimeout);
    EXPECT_EQ(fsm.state(), CallFsmState::Closed);
}

TEST_F(CallFsmTest, RtpInactivity_MovesToClosing) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    ASSERT_EQ(fsm.state(), CallFsmState::Listening);

    fsm.on_timer_fired(FsmTimerType::RtpInactivity);
    EXPECT_EQ(fsm.state(), CallFsmState::Closing);
}

// ── Guard conditions ──────────────────────────────────────────────────────────

TEST_F(CallFsmTest, InvalidTransition_IsNoOp) {
    auto fsm = make_fsm();
    ASSERT_EQ(fsm.state(), CallFsmState::Idle);

    // Calling stt_final while Idle — must not crash or change state
    fsm.on_stt_final("hello", 0.9f);
    EXPECT_EQ(fsm.state(), CallFsmState::Idle);
    EXPECT_TRUE(transitions_.empty());
}

TEST_F(CallFsmTest, ServiceReady_WrongState_IsNoOp) {
    auto fsm = make_fsm();
    ASSERT_EQ(fsm.state(), CallFsmState::Idle);

    fsm.on_service_ready();  // guard: must be Connecting
    EXPECT_EQ(fsm.state(), CallFsmState::Idle);
}

// ── can_accept_audio ──────────────────────────────────────────────────────────

TEST_F(CallFsmTest, CanAcceptAudio_InValidStates) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();
    EXPECT_TRUE(fsm.can_accept_audio());   // Listening

    fsm.on_speech_started(0.3f);
    EXPECT_TRUE(fsm.can_accept_audio());   // Recognizing

    fsm.on_stt_final("hi", 0.9f);
    EXPECT_TRUE(fsm.can_accept_audio());   // Thinking — VAD must run for early barge-in

    fsm.on_text_ready();
    EXPECT_TRUE(fsm.can_accept_audio());   // Synthesizing — VAD must run for early barge-in

    fsm.on_first_audio_chunk();
    EXPECT_TRUE(fsm.can_accept_audio());   // Speaking (barge-in detection)

    fsm.on_playback_finished(true);
    EXPECT_TRUE(fsm.can_accept_audio());   // BargeIn (barge-in detection)
}

// ── Timer cancellation ────────────────────────────────────────────────────────

TEST_F(CallFsmTest, ExitCancelsActiveTimer) {
    auto fsm = make_fsm();
    fsm.on_session_start();          // schedules ConnectionTimeout (id=1)
    ASSERT_TRUE(cancelled_timers_.empty());  // nothing cancelled yet

    fsm.on_service_ready();          // exits Connecting → cancels timer id=1
    EXPECT_FALSE(cancelled_timers_.empty());
    EXPECT_EQ(cancelled_timers_[0], FsmTimerId{1});
}

// ── State duration measured ───────────────────────────────────────────────────

TEST_F(CallFsmTest, StateDurationObserved_OnEveryTransition) {
    auto fsm = make_fsm();
    fsm.on_session_start();
    fsm.on_service_ready();

    // Two transitions happened → two duration observations
    EXPECT_GE(metrics_.observations.size(), 2u);
    for (const auto& [name, _] : metrics_.observations)
        EXPECT_EQ(name, "fsm.state_duration_ms");
}

} // namespace
