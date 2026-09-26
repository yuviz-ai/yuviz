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

// Control-plane object: one per active WebSocket connection.
// Owns MediaSession (data plane), CallFSM (state machine), and the
// IConversationTransport for this session.
// AudioWorkerPool holds a raw pointer to the MediaSession for hot-path draining.
//
// Threading model (R3):
//   All CallFSM trigger methods run on a dedicated per-session control thread.
//   Other threads (AudioWorker, TimerService, gRPC) post std::function<void()>
//   callables to the per-session control queue via post().  This makes CallFSM
//   single-threaded without any mutex, eliminating TS-1, TS-2, and TS-3.
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

    // Called by lws receive callback — push raw PCM into the MediaSession ring.
    // Safe to call from any thread (reads fsm_->can_accept_audio() atomically).
    void push_inbound_audio(const uint8_t* data, size_t len) noexcept;

    // Post a clean shutdown request to the control queue and close the connection.
    // Safe to call from any thread.
    void terminate(const std::string& reason = "caller_hangup");

    // Post a DTMF digit to the control queue for call-flow menu navigation.
    // Called by EslEventListener when FreeSWITCH reports a DTMF event.
    // Safe to call from any thread.
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

    // Post a callable to the per-session control queue.  Safe from any thread.
    void post(std::function<void()> fn);

    // Control thread body: drains the control queue until stopped.
    void control_loop() noexcept;

    SessionContext                          ctx_;
    std::shared_ptr<IWebSocketConnection>   connection_;
    std::unique_ptr<MediaSession>           media_;
    AudioWorkerPool&                        pool_;
    std::unique_ptr<IConversationTransport> transport_;
    ITimerService&                          timer_svc_;
    IDispatcher&                            dispatcher_;
    IMetrics&                               metrics_;
    SessionMetrics                          sm_;       // pre-labels metrics with tenant_id
    IClock&                                 clock_;
    Logger&                                 logger_;
    ContextLogger                           log_;      // prepends all 5 obs IDs to every line
    EslClient&                              esl_client_;
    TransferCorrelator&                     transfer_correlator_;
    // bgapi Job-UUID keyspace — see docs/warm_transfer_architecture.md §3;
    // shares the exact same TransferCorrelator class as transfer_correlator_
    // above (a second instance, not a second implementation — see
    // Application.h's declaration comment for why one class covers both
    // keyspaces).
    TransferCorrelator&                     job_correlator_;
    // Set at watch registration to "transfer_timeout" (the default when
    // nothing else ever resolves it — see CallFSM's own TransferTimeout
    // path) and overwritten with the real cause wherever this class itself
    // resolves the outcome (a CHANNEL_BRIDGE/CHANNEL_HANGUP event, or an
    // immediately-rejected uuid_transfer command). Read once, in
    // h.on_transfer_completed, to fill TransferFailed's reason field —
    // control-thread-only, like the barge-in/end-call state above.
    std::string pending_transfer_detail_;
    // Per-attempt observability state, control-thread-only like
    // pending_transfer_detail_ above: the correlation id from the
    // TransferRequest gRPC message (Task 4 of transfer hardening — echoed
    // back on TransferInitiated/Completed/Failed), the destination (kept so
    // the outcome log can name it even when CallFSM's TransferTimeout path
    // resolves with no destination of its own), and the attempt start time
    // for the duration_ms log/metric.
    std::string       active_transfer_id_;
    std::string       active_transfer_destination_;
    // "cold" | "warm" — set alongside active_transfer_id_/destination in
    // cbs.on_transfer_requested (the transport callback, which is the only
    // place transfer_type actually arrives from the Conversation Service);
    // read once, in h.on_transfer_requested, to fill TransferInitiated's
    // type field and to select active_transfer_ below.
    std::string       active_transfer_type_;
    // Set alongside active_transfer_type_ above, in cbs.on_transfer_requested
    // — see TransferCoordinatorContext::caller_id/waiting_experience's own
    // comments for what each controls. active_caller_id_ is resolved to
    // ctx_.caller_did (the caller's own ANI) here if the Conversation
    // Service sent an empty string, rather than pushing that fallback into
    // WarmTransferCoordinator — CallSession already has both values.
    std::string       active_caller_id_;
    std::string       active_waiting_experience_;
    IClock::TimePoint transfer_started_at_{};

    // Both transfer strategies as value members (no allocation, lifetime
    // tied to the session's own — see docs/warm_transfer_architecture.md
    // "Coordinator lifetime"). active_transfer_ is a non-owning pointer to
    // whichever applies for the current attempt, selected in
    // cbs.on_transfer_requested; nullptr when no transfer is in flight.
    // Declared here — AFTER esl_client_/transfer_correlator_/job_correlator_
    // above — so that during ~CallSession() they are destroyed BEFORE those
    // dependencies (reverse declaration order), the second of two
    // independent safety layers against a coordinator callback firing into
    // an already-destroyed dependency (the first, primary layer is the
    // explicit active_transfer_->shutdown() call in ~CallSession() itself).
    ColdTransferCoordinator cold_transfer_;
    WarmTransferCoordinator warm_transfer_;
    // Atomic: written on the control thread (cbs.on_transfer_requested,
    // h.on_transfer_completed) and read from ~CallSession()'s bounded
    // wait-loop, which runs on session_cleanup_pool_'s thread, not the
    // control thread — see that destructor's own comment. A plain pointer
    // here produced a genuine, confirmed-live data race (SIGSEGV, 2026-07-
    // 20) once session teardown started running off the control thread.
    // std::atomic<T*> has no operator->, so call sites load() into a local
    // first rather than dereferencing through the member directly.
    std::atomic<ITransferCoordinator*> active_transfer_{nullptr};

    std::optional<CallFSM> fsm_;

    // Declared after media_ and connection_: PlaybackDrain holds references to both.
    PlaybackDrain playback_drain_;

    // ── Control queue ────────────────────────────────────────────────────────
    // MPSC: AudioWorker, TimerService, gRPC threads post; control_thread_ consumes.
    std::deque<std::function<void()>> control_queue_;
    std::mutex                        control_mutex_;
    std::condition_variable           control_cv_;
    bool                              control_stopping_{false};
    std::thread                       control_thread_;

    // ── Timer ID translation ─────────────────────────────────────────────────
    // CallFSM and ITimerService are intentionally decoupled: the FSM only
    // knows an opaque FsmTimerId, while ITimerService issues its own TimerId
    // space (monotonic across all sessions/types).  timer_map_ bridges the
    // two; entries are inserted by schedule_timer, erased by cancel_timer or
    // the ~CallSession sweep.  Both are invoked through the control queue, so
    // timer_map_ needs no synchronisation of its own.
    std::unordered_map<FsmTimerId, TimerId> timer_map_;
    FsmTimerId next_fsm_timer_id_{1};

    // Set true by on_tts_started and consumed (false) by the first on_tts_chunk.
    // Prevents posting on_first_audio_chunk() for every TTS frame when only the
    // first matters; set/load happen on the gRPC reader thread (serialised by
    // the reader loop) so no additional synchronisation is needed.
    std::atomic<bool> tts_first_chunk_pending_{false};

    // Set (via active_transfer_'s on_media_handoff callback — see
    // ITransferCoordinator.h) the moment a transfer attempt commits to
    // handing the customer's own SIP leg to something outside the
    // Gateway's control. h.on_session_close reads this to decide whether
    // esl_client_.hangup() would disconnect a customer who is now (or is
    // about to be) live with a real human — see that handler's own
    // comment. Written on whichever thread the coordinator's event fires
    // on (EslEventListener's, for warm), read on the thread that reacts to
    // the resulting WebSocket disconnect — hence atomic, not control-
    // thread-only like most of this class's other flags.
    std::atomic<bool> sip_leg_handed_off_{false};

    // ── Barge-in capture & pre-roll (control-thread only) ────────────────────
    // Audio reaches the ConversationService only in Recognizing.  Frames spoken
    // during a barge-in are held in barge_in_buffer_ and flushed when
    // BargeIn→Listening completes; preroll_ is a rolling window of recent
    // unforwarded frames flushed on entry to Recognizing so utterances keep
    // their first word (SpeechStart fires onset_ms after speech begins).
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

    // Set by the transport's on_end_call callback (agent decided the call is
    // over); consumed in the Speaking→Listening on_playback_finished handler
    // once the goodbye TTS has actually finished playing.  A barge-in during
    // that final playback clears it — the caller talking means the agent's
    // decision is stale.  Control-thread-only, like the barge-in state above.
    bool pending_end_call_{false};
    // EndCall.grace_period_ms from the same message; {} (zero) = let the FSM
    // fall back to its own configured default.  Consumed alongside
    // pending_end_call_.
    std::chrono::milliseconds pending_goodbye_timeout_{};
};

} // namespace voiceai
