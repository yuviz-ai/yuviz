#pragma once

#include "common/IClock.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "metrics/IMetrics.h"
#include "session/CallFsmTimerConfig.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <string>
#include <string_view>

namespace voiceai {

// Each state has exactly one owner — the subsystem responsible for producing
// the exit event that drives the transition out of that state.
//
//   Idle         —  no owner (initial)
//   Connecting   —  GrpcConversationTransport  (exits on ServiceReady)
//   Listening    —  EnergyVAD / AudioWorker    (exits on SpeechStarted)
//   Recognizing  —  STT provider               (exits on STTFinal)
//   Thinking     —  WorkflowEngine+AgentRuntime(exits on TextReady)
//   Synthesizing —  TTS provider               (exits on FirstAudioChunk)
//   Speaking     —  PlaybackQueue              (exits on PlaybackFinished)
//   WaitingForHangup — Gateway (ESL)           (exits on SpeechStarted or GoodbyeTimeout)
//   BargeIn      —  Gateway                    (exits on CancelComplete)
//   Transferring —  TransferManager            (exits on TransferCompleted)
//   Finalizing   —  ConversationService         (exits on ConversationFinalized or FinalizingTimeout)
//   Closing      —  SessionManager             (exits on CloseAcknowledged)
//   Closed       —  no owner (terminal)
//
// WaitingForHangup is Gateway-only (no Python ConversationFSM mirror).
// Finalizing follows only a successful transfer, so the gRPC stream stays open
// until the Conversation Service finishes post-call cleanup.

enum class CallFsmState : uint8_t {
    Idle,
    Connecting,
    Listening,
    Recognizing,
    Thinking,
    Synthesizing,
    Speaking,
    WaitingForHangup,
    BargeIn,
    Transferring,
    Finalizing,
    Closing,
    Closed,
};

[[nodiscard]] std::string_view to_string(CallFsmState s) noexcept;

// One active timer per state.
enum class FsmTimerType : uint8_t {
    ConnectionTimeout,   // Connecting
    NoSpeechTimeout,     // Listening
    MaxUtteranceTimeout, // Recognizing, before speech_ended (bounds raw talk time)
    SttTimeout,          // Recognizing, after speech_ended (bounds STT response time)
    LlmTimeout,          // Thinking
    TtsTimeout,          // Synthesizing
    PlaybackTimeout,     // Speaking
    GoodbyeTimeout,      // WaitingForHangup
    GoodbyeConfirm,      // WaitingForHangup, after a SpeechStarted onset
    BargeInWindow,       // BargeIn
    TransferTimeout,     // Transferring
    FinalizingTimeout,   // Finalizing
    CloseTimeout,        // Closing
    RtpInactivity,       // Any  — heartbeat; fires to Closing
};

using FsmTimerId = uint64_t;
static constexpr FsmTimerId kNoTimer = 0;

// Decouples the FSM from ITimerService and IDispatcher.
struct CallFsmHandlers {
    std::function<FsmTimerId(FsmTimerType, std::chrono::milliseconds)> schedule_timer;
    std::function<void(FsmTimerId)>                                     cancel_timer;

    std::function<void(CallFsmState from,
                       CallFsmState to,
                       std::string_view trigger,
                       double duration_ms)>                              on_state_changed;

    std::function<void(float energy_db)>                             on_speech_started;
    std::function<void(uint32_t duration_ms, float energy_db)>      on_speech_ended;
    std::function<void(std::string text, float confidence)>         on_stt_final;
    std::function<void(bool interrupted)>                            on_playback_finished;
    std::function<void(std::string queue_id, std::string reason)>   on_transfer_requested;
    std::function<void(bool success, std::string transfer_id)>      on_transfer_completed;
    // Fired once when Finalizing exits (ack or FinalizingTimeout).
    std::function<void()>                                            on_conversation_finalized;
    std::function<void(std::string reason)>                         on_session_close;
};

class CallFSM : private NonCopyable, private NonMovable {
public:
    CallFSM(std::string         session_id,
            CallFsmHandlers     handlers,
            CallFsmTimerConfig  timer_cfg,
            IMetrics&           metrics,
            IClock&             clock,
            Logger&             logger);

    // Named after the event that causes the transition, not the resulting
    // state.  Each guards against invalid source states and no-ops if wrong.

    void on_session_start();                                      // Idle       → Connecting
    void on_service_ready();                                      // Connecting → Listening
    void on_speech_started(float energy_db);                      // Listening  → Recognizing
    // Not a transition (stays in Recognizing); only relays on_speech_ended.
    void notify_speech_ended(uint32_t duration_ms, float energy_db);
    void on_stt_final(std::string text, float confidence);        // Recognizing→ Thinking
    void on_text_ready();                                         // Thinking   → Synthesizing
    void on_first_audio_chunk();                                  // Synthesizing→ Speaking
    // end_call_pending && !interrupted → WaitingForHangup. A zero
    // goodbye_timeout_override means use timer_cfg_.goodbye_timeout.
    void on_playback_finished(
        bool interrupted,
        bool end_call_pending = false,
        std::chrono::milliseconds goodbye_timeout_override = {}); // Speaking → Listening | WaitingForHangup | BargeIn
    void on_cancel_complete();                                    // BargeIn    → Listening
    void on_early_barge_in();                                   // Thinking|Synthesizing → BargeIn
    void on_transfer_requested(std::string queue_id,
                                std::string reason);              // Any active → Transferring
    void on_transfer_completed(bool success,
                                std::string transfer_id);         // Transferring→ Finalizing (success) | Closing (failure)
    void on_conversation_finalized();                             // Finalizing → Closing
    void on_session_close(std::string reason);                    // Any active → Closing
    void on_close_acknowledged();                                 // Closing    → Closed

    void on_timer_fired(FsmTimerType type);

    // ── Query ─────────────────────────────────────────────────────────────────

    [[nodiscard]] CallFsmState     state()           const noexcept;
    [[nodiscard]] std::string_view state_name()      const noexcept;
    [[nodiscard]] bool             is_terminal()     const noexcept;
    [[nodiscard]] bool             can_accept_audio()const noexcept;

private:
    void transition(CallFsmState next, std::string_view trigger);
    void on_enter(CallFsmState s);
    void on_exit(CallFsmState s);
    [[nodiscard]] static bool is_valid(CallFsmState from, CallFsmState to) noexcept;
    [[nodiscard]] bool        is_active() const noexcept;

    void do_session_close_(std::string_view reason);
    void do_cancel_complete_();
    void do_transfer_completed_(bool success, std::string transfer_id);
    void do_conversation_finalized_();

    std::string         session_id_;
    CallFsmHandlers     handlers_;
    CallFsmTimerConfig  timer_cfg_;
    IMetrics&           metrics_;
    IClock&             clock_;
    Logger&             logger_;

    // All triggers run on CallSession's control thread, so no mutex; state_ is
    // atomic only for lock-free reads from other threads.
    std::atomic<CallFsmState>  state_{CallFsmState::Idle};
    IClock::TimePoint          state_entered_at_;
    FsmTimerId                 active_timer_{kNoTimer};

    // Handed from on_playback_finished() to on_enter(WaitingForHangup); zero = default.
    std::chrono::milliseconds pending_goodbye_timeout_override_{};

    // True while GoodbyeConfirm is pending after a WaitingForHangup speech onset.
    bool  awaiting_goodbye_confirm_{false};
    float pending_goodbye_energy_db_{0.0f};
};

} // namespace voiceai
