#pragma once

#include "common/IClock.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "dispatcher/IDispatcher.h"
#include "logging/ContextLogger.h"
#include "logging/Logger.h"
#include "media/AudioWorkerPool.h"
#include "media/MediaSession.h"
#include "media/PlaybackDrain.h"
#include "metrics/IMetrics.h"
#include "metrics/SessionMetrics.h"
#include "session/CallFSM.h"
#include "session/SessionContext.h"
#include "telephony/ColdTransferCoordinator.h"
#include "telephony/EslClient.h"
#include "telephony/ITransferCoordinator.h"
#include "telephony/TransferCorrelator.h"
#include "telephony/WarmTransferCoordinator.h"
#include "timer/ITimerService.h"
#include "transport/IConversationTransport.h"
#include "websocket/IWebSocketConnection.h"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <unordered_map>

namespace voiceai {

// Control-plane object, one per WebSocket connection. All CallFSM triggers run on
// a per-session control thread; other threads post() to it, so the FSM needs no mutex.
class CallSession : private NonCopyable, private NonMovable {
public:
    CallSession(SessionContext                        ctx,
                std::shared_ptr<IWebSocketConnection> connection,
                std::unique_ptr<MediaSession>         media,
                AudioWorkerPool&                      worker_pool,
                std::unique_ptr<IConversationTransport> transport,
                ITimerService&                        timer_svc,
                IDispatcher&                          dispatcher,
                IMetrics&                             metrics,
                IClock&                               clock,
                Logger&                               logger,
                EslClient&                            esl_client,
                TransferCorrelator&                    transfer_correlator,
                TransferCorrelator&                    job_correlator);

    ~CallSession();

    // Thread-safe.
    void push_inbound_audio(const uint8_t* data, size_t len) noexcept;

    // Thread-safe; posts shutdown and closes the connection.
    void terminate(const std::string& reason = "caller_hangup");

    // Thread-safe.
    void push_dtmf(const std::string& digit);

    [[nodiscard]] const std::string& session_id() const noexcept;
    [[nodiscard]] CallFsmState       fsm_state()  const noexcept;
    [[nodiscard]] bool               is_terminal() const noexcept;
    [[nodiscard]] MediaSession&      media_session() noexcept { return *media_; }

private:
    void wire_fsm_handlers();
    void wire_transport_callbacks();
    void wire_media_callbacks();
    void wire_connection_callbacks();

    void on_text_message(const std::string& msg);

    void on_fsm_state_changed(CallFsmState from, CallFsmState to,
                               std::string_view trigger, double duration_ms);

    // Thread-safe.
    void post(std::function<void()> fn);

    void control_loop() noexcept;

    SessionContext                          ctx_;
    std::shared_ptr<IWebSocketConnection>   connection_;
    std::unique_ptr<MediaSession>           media_;
    AudioWorkerPool&                        pool_;
    std::unique_ptr<IConversationTransport> transport_;
    ITimerService&                          timer_svc_;
    IDispatcher&                            dispatcher_;
    IMetrics&                               metrics_;
    SessionMetrics                          sm_;
    IClock&                                 clock_;
    Logger&                                 logger_;
    ContextLogger                           log_;
    EslClient&                              esl_client_;
    TransferCorrelator&                     transfer_correlator_;
    TransferCorrelator&                     job_correlator_;  // bgapi Job-UUIDs

    // Transfer-attempt state below is control-thread-only unless atomic.
    // Defaults to "transfer_timeout"; overwritten with the real failure cause.
    std::string pending_transfer_detail_;
    std::string       active_transfer_id_;
    std::string       active_transfer_destination_;
    std::string       active_transfer_type_;  // "cold" | "warm"
    // Falls back to ctx_.caller_did when the Conversation Service sends empty.
    std::string       active_caller_id_;
    std::string       active_waiting_experience_;
    IClock::TimePoint transfer_started_at_{};

    // Declared after esl_client_ and the correlators so they're destroyed first.
    ColdTransferCoordinator cold_transfer_;
    WarmTransferCoordinator warm_transfer_;
    // Non-owning; nullptr when idle. Atomic because ~CallSession() reads it off
    // the control thread.
    std::atomic<ITransferCoordinator*> active_transfer_{nullptr};

    std::optional<CallFSM> fsm_;

    // Declared after media_ and connection_: PlaybackDrain holds references to both.
    PlaybackDrain playback_drain_;

    // ── Control queue (MPSC) ─────────────────────────────────────────────────
    std::deque<std::function<void()>> control_queue_;
    std::mutex                        control_mutex_;
    std::condition_variable           control_cv_;
    bool                              control_stopping_{false};
    std::thread                       control_thread_;

    // ── Timer ID translation ─────────────────────────────────────────────────
    // Maps the FSM's opaque timer ids to ITimerService ids; control-thread only.
    std::unordered_map<FsmTimerId, TimerId> timer_map_;
    FsmTimerId next_fsm_timer_id_{1};

    // Ensures on_first_audio_chunk() is posted once per TTS response.
    std::atomic<bool> tts_first_chunk_pending_{false};

    // Set once a transfer hands the customer's SIP leg outside the Gateway, so
    // session close must not hang it up. Written/read on different threads.
    std::atomic<bool> sip_leg_handed_off_{false};

    // ── Barge-in capture & pre-roll (control-thread only) ────────────────────
    // Audio is forwarded only in Recognizing. barge_in_buffer_ holds barge-in speech;
    // preroll_ keeps the first word, since SpeechStart fires onset_ms late.
    static constexpr size_t kMaxBargeInFrames = 500;  // 10 s at 20 ms/frame
    static constexpr size_t kPrerollFrames    = 25;   // 500 ms at 20 ms/frame
    std::vector<AudioFrame> barge_in_buffer_;
    std::deque<AudioFrame>  preroll_;
    bool  barge_in_capture_{false};
    float barge_in_energy_db_{0.0f};
    // SpeechEnd that fired during the BargeIn window — replayed after the flush.
    bool     pending_speech_ended_{false};
    uint32_t pending_se_duration_ms_{0};
    float    pending_se_energy_db_{0.0f};

    // Agent ended the call; applied once the goodbye finishes playing. Barge-in clears it.
    bool pending_end_call_{false};
    // EndCall.grace_period_ms; zero = FSM default.
    std::chrono::milliseconds pending_goodbye_timeout_{};
};

} // namespace voiceai
