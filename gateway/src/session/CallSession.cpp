#include "session/CallSession.h"
#include "common/ThreadUtils.h"
#include "events/SessionEvent.h"
#include "timer/TimerType.h"

#include <stdexcept>

namespace voiceai {

namespace {

TimerType to_timer_type(FsmTimerType t) noexcept {
    switch (t) {
    case FsmTimerType::ConnectionTimeout: return TimerType::ConnectionTimeout;
    case FsmTimerType::NoSpeechTimeout:   return TimerType::NoSpeechTimeout;
    case FsmTimerType::MaxUtteranceTimeout: return TimerType::MaxUtteranceTimeout;
    case FsmTimerType::SttTimeout:        return TimerType::SttTimeout;
    case FsmTimerType::LlmTimeout:        return TimerType::LlmTimeout;
    case FsmTimerType::TtsTimeout:        return TimerType::TtsTimeout;
    case FsmTimerType::PlaybackTimeout:   return TimerType::PlaybackTimeout;
    case FsmTimerType::GoodbyeTimeout:    return TimerType::GoodbyeTimeout;
    case FsmTimerType::GoodbyeConfirm:    return TimerType::GoodbyeConfirm;
    case FsmTimerType::BargeInWindow:     return TimerType::BargeInWindow;
    case FsmTimerType::TransferTimeout:   return TimerType::TransferTimeout;
    case FsmTimerType::FinalizingTimeout: return TimerType::FinalizingTimeout;
    case FsmTimerType::CloseTimeout:      return TimerType::CloseTimeout;
    case FsmTimerType::RtpInactivity:     return TimerType::RtpInactivity;
    }
    return TimerType::CloseTimeout;
}

} // namespace

CallSession::CallSession(SessionContext                        ctx,
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
                         TransferCorrelator&                    job_correlator)
    : ctx_           (std::move(ctx))
    , connection_    (std::move(connection))
    , media_         (std::move(media))
    , pool_          (worker_pool)
    , transport_     (std::move(transport))
    , timer_svc_     (timer_svc)
    , dispatcher_    (dispatcher)
    , metrics_       (metrics)
    , sm_            (metrics_, ctx_.obs.tenant_id)
    , clock_         (clock)
    , logger_        (logger)
    , log_           (ctx_.obs, logger_)
    , esl_client_    (esl_client)
    , transfer_correlator_(transfer_correlator)
    , job_correlator_(job_correlator)
    , cold_transfer_(esl_client_, transfer_correlator_, log_)
    , warm_transfer_(esl_client_, transfer_correlator_, job_correlator_, log_)
    , playback_drain_(media_->playback_queue(), *connection_, logger_)
{
    wire_fsm_handlers();
    wire_transport_callbacks();
    wire_media_callbacks();
    wire_connection_callbacks();

    // Media callbacks can fire right after assign(), so the control thread must exist first.
    control_thread_ = std::thread([this] {
        set_thread_name("Ctrl-" + ctx_.obs.session_id.substr(0, 8));
        control_loop();
    });

    pool_.assign(media_.get());

    // Before the transport, so TTS chunks during the handshake are consumed.
    playback_drain_.start();
    transport_->start();

    post([this] { fsm_->on_session_start(); });
    transport_->open_session(ctx_);
}

CallSession::~CallSession() {
    // -1. stop_audio_fork() closes the WebSocket (triggering this destructor) before a
    //     warm transfer finishes; wait briefly so TransferCompleted still gets sent.
    if (ITransferCoordinator* t = active_transfer_.load(std::memory_order_acquire)) {
        constexpr auto kMaxWait      = std::chrono::milliseconds{3000};
        constexpr auto kPollInterval = std::chrono::milliseconds{10};
        const auto deadline = std::chrono::steady_clock::now() + kMaxWait;
        while (t->state() == CoordinatorState::Active &&
               std::chrono::steady_clock::now() < deadline) {
            std::this_thread::sleep_for(kPollInterval);
        }
    }

    // 0. Coordinator watches capture `this`; shutdown() ensures no late ESL event
    //    calls back into a destroyed session.
    if (ITransferCoordinator* t = active_transfer_.load(std::memory_order_acquire)) {
        t->shutdown();
        active_transfer_.store(nullptr, std::memory_order_release);
    }

    // 1. Stop AudioWorker — no more media callbacks will be posted.
    pool_.unassign(media_.get());

    // 2. Joins gRPC threads; no transport callbacks after this.
    transport_->stop();

    // 3. Close the FSM and cancel timers on the control thread before stopping it,
    //    so no timer callback can post() into a destroyed queue.
    {
        std::lock_guard lock{control_mutex_};
        control_queue_.push_back([this] {
            // Close first: Closing schedules a CloseTimeout that must also be cancelled.
            if (fsm_ && !fsm_->is_terminal())
                fsm_->on_session_close("session_destroyed");
            for (auto& [fid, tid] : timer_map_)
                timer_svc_.cancel(tid);
            timer_map_.clear();
        });
        control_stopping_ = true;
    }
    control_cv_.notify_all();
    if (control_thread_.joinable()) control_thread_.join();

    playback_drain_.stop();

    // Idempotent; stop() already closed the session.
    transport_->close_session(ctx_.obs.session_id);
}

void CallSession::push_inbound_audio(const uint8_t* data, size_t len) noexcept {
    if (fsm_ && fsm_->can_accept_audio())
        media_->push_inbound(data, len);
}

void CallSession::terminate(const std::string& reason) {
    post([this, r = reason] {
        if (fsm_ && !fsm_->is_terminal())
            fsm_->on_session_close(r);
    });
}

void CallSession::push_dtmf(const std::string& digit) {
    if (transport_)
        transport_->send_dtmf(ctx_.obs.session_id, digit);
}

const std::string& CallSession::session_id() const noexcept {
    return ctx_.obs.session_id;
}

CallFsmState CallSession::fsm_state() const noexcept {
    return fsm_ ? fsm_->state() : CallFsmState::Closed;
}

bool CallSession::is_terminal() const noexcept {
    return !fsm_ || fsm_->is_terminal();
}

void CallSession::post(std::function<void()> fn) {
    {
        std::lock_guard lock{control_mutex_};
        control_queue_.push_back(std::move(fn));
    }
    control_cv_.notify_one();
}

void CallSession::control_loop() noexcept {
    for (;;) {
        std::function<void()> fn;
        {
            std::unique_lock lock{control_mutex_};
            control_cv_.wait(lock, [this] {
                return !control_queue_.empty() || control_stopping_;
            });
            if (control_stopping_ && control_queue_.empty()) break;
            fn = std::move(control_queue_.front());
            control_queue_.pop_front();
        }
        try {
            fn();
        } catch (const std::exception& e) {
            log_.error("control_loop: exception what={}", e.what());
        } catch (...) {
            log_.error("control_loop: unknown exception");
        }
    }
}

void CallSession::wire_fsm_handlers() {
    const std::string& sid = ctx_.obs.session_id;
    const TenantConfig& tc = *ctx_.tenant;

    CallFsmHandlers h;

    h.schedule_timer = [this, sid](FsmTimerType ftype,
                                   std::chrono::milliseconds delay) -> FsmTimerId
    {
        const FsmTimerId fid   = next_fsm_timer_id_++;
        const TimerType  ttype = to_timer_type(ftype);

        const TimerId tid = timer_svc_.schedule(
            ttype, sid, delay,
            [this, ftype](TimerId, TimerType, const std::string&) {
                post([this, ftype] { if (fsm_) fsm_->on_timer_fired(ftype); });
            });
        timer_map_[fid] = tid;
        return fid;
    };

    h.cancel_timer = [this](FsmTimerId fid) {
        auto it = timer_map_.find(fid);
        if (it != timer_map_.end()) {
            timer_svc_.cancel(it->second);
            timer_map_.erase(it);
        }
    };

    h.on_state_changed = [this](CallFsmState from, CallFsmState to,
                                 std::string_view trigger, double duration_ms) {
        on_fsm_state_changed(from, to, trigger, duration_ms);
    };

    h.on_speech_started = [](float /*energy_db*/) {};

    h.on_stt_final = [this](std::string text, float confidence) {
        log_.info("stt_final text=\"{}\" confidence={:.2f}", text, confidence);
        log_.set_provider_request_id(ctx_.obs.trace_id + "-stt");
    };

    h.on_playback_finished = [this](bool interrupted) {
        transport_->send_playback_finished(ctx_.obs.session_id, interrupted);
        log_.set_provider_request_id("");
    };

    h.on_speech_ended = [this](uint32_t duration_ms, float energy_db) {
        transport_->send_speech_ended(ctx_.obs.session_id, duration_ms, energy_db);
    };

    // Runs before the FSM enters Transferring; post() defers the body until it has,
    // so fsm_->on_transfer_completed() is valid.
    h.on_transfer_requested = [this](std::string queue_id, std::string reason) {
        log_.info("Transfer requested destination={} reason={} transfer_id={}",
                  queue_id, reason, active_transfer_id_);
        post([this, destination = std::move(queue_id), reason = std::move(reason)]() mutable {
            const std::string call_id = ctx_.obs.call_id;
            transfer_started_at_ = clock_.now();
            sm_.increment("transfers.attempted");

            // Defense in depth; the Conversation Service shouldn't send this.
            if (destination.empty()) {
                log_.error("Transfer requested with empty destination — failing attempt "
                          "transfer_id={} (check the agent's transfer_destination config)",
                          active_transfer_id_);
                transport_->send_transfer_initiated(ctx_.obs.session_id, active_transfer_type_,
                                                    destination, reason, active_transfer_id_);
                pending_transfer_detail_ = "empty_destination";
                if (fsm_) fsm_->on_transfer_completed(false, destination);
                return;
            }

            // Sent first: the service's only chance to react before the AI session closes.
            transport_->send_transfer_initiated(ctx_.obs.session_id, active_transfer_type_,
                                                destination, reason, active_transfer_id_);

            // Stands if only CallFSM's TransferTimeout resolves the attempt.
            pending_transfer_detail_ = "transfer_timeout";

            TransferCoordinatorContext tctx{call_id, destination, reason, active_transfer_id_,
                                           active_caller_id_, active_waiting_experience_};
            TransferCoordinatorCallbacks tcbs;
            // Not posted: must be visible before the WebSocket disconnect it causes.
            tcbs.on_media_handoff = [this] {
                sip_leg_handed_off_.store(true, std::memory_order_relaxed);
            };
            tcbs.on_transfer_completed =
                [this](bool success, std::string dest, std::string detail) {
                    // May fire on the ESL thread or synchronously inside start();
                    // post() keeps it on the control thread and avoids re-entrancy.
                    post([this, success, dest = std::move(dest), detail = std::move(detail)]() mutable {
                        pending_transfer_detail_ = std::move(detail);
                        if (fsm_) fsm_->on_transfer_completed(success, std::move(dest));
                    });
                };
            active_transfer_.load(std::memory_order_acquire)->start(std::move(tctx), std::move(tcbs));
        });
    };

    // Single completion point for every transfer outcome. Must not close the AI
    // session: success waits for ConversationFinalized, failure streams an apology.
    h.on_transfer_completed = [this](bool success, std::string destination) {
        // TransferTimeout resolves with no destination.
        if (destination.empty()) destination = active_transfer_destination_;
        const auto duration_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            clock_.now() - transfer_started_at_).count();
        const bool timed_out = !success && pending_transfer_detail_ == "transfer_timeout";
        log_.info("Transfer completed success={} destination={} detail={} transfer_id={} duration_ms={}",
                  success, destination, success ? "bridged" : pending_transfer_detail_,
                  active_transfer_id_, duration_ms);
        sm_.increment(success ? "transfers.succeeded"
                              : (timed_out ? "transfers.timeout" : "transfers.failed"));
        sm_.observe("transfer.duration_ms", static_cast<double>(duration_ms));
        if (ITransferCoordinator* t = active_transfer_.load(std::memory_order_acquire)) {
            t->shutdown();
            active_transfer_.store(nullptr, std::memory_order_release);
        }
        if (success) {
            transport_->send_transfer_completed(ctx_.obs.session_id, destination,
                                                active_transfer_id_);
        } else {
            transport_->send_transfer_failed(ctx_.obs.session_id, destination,
                                             pending_transfer_detail_, active_transfer_id_);
        }
    };

    h.on_conversation_finalized = [this] {
        log_.info("Conversation finalized — closing AI session");
        transport_->close_session(ctx_.obs.session_id);
    };

    // Every path into Closing, including timers, must close the WebSocket and send
    // the SIP BYE (closing the WebSocket alone only stops audio).
    h.on_session_close = [this](std::string reason) {
        log_.info("Session close reason={}", reason);
        connection_->close();
        // Don't hang up a customer already handed off to a human.
        if (sip_leg_handed_off_.load(std::memory_order_relaxed)) {
            log_.info("Skipping esl_client_.hangup() — SIP leg already handed off reason={}",
                     reason);
            return;
        }
        esl_client_.hangup(ctx_.obs.call_id, reason);
    };

    const CallFsmTimerConfig& timer_cfg = tc.timers;

    fsm_.emplace(ctx_.obs.session_id, std::move(h), timer_cfg,
                 metrics_, clock_, logger_);
}

void CallSession::wire_transport_callbacks() {
    ConversationTransportCallbacks cbs;

    cbs.on_service_ready = [this](const std::string& /*session_id*/) {
        post([this] { if (fsm_) fsm_->on_service_ready(); });
    };

    cbs.on_stt_result = [this](std::string text, float confidence) {
        post([this, t = std::move(text), confidence]() mutable {
            if (fsm_) fsm_->on_stt_final(std::move(t), confidence);
        });
    };

    cbs.on_tts_started = [this] {
        tts_first_chunk_pending_.store(true, std::memory_order_relaxed);
        post([this] { if (fsm_) fsm_->on_text_ready(); });
    };

    cbs.on_tts_chunk = [this](AudioFrame frame) {
        // Only the first chunk of a turn drives Synthesizing→Speaking.
        if (tts_first_chunk_pending_.exchange(false, std::memory_order_relaxed)) {
            post([this] { if (fsm_) fsm_->on_first_audio_chunk(); });
        }

        // Split into 20ms sub-frames: cancel_playback() can only remove frames
        // still queued, so frame size bounds barge-in stop latency.
        constexpr uint32_t kFrameMs    = 20;
        const     uint32_t sr          = frame.sample_rate > 0 ? frame.sample_rate
                                                                 : 16'000u;
        const size_t frame_bytes = static_cast<size_t>(sr) * kFrameMs / 1000 * 2; // PCM S16LE

        const size_t total = frame.payload.size();
        if (total <= frame_bytes) {
            media_->push_outbound(std::move(frame));
            return;
        }

        for (size_t off = 0; off < total; off += frame_bytes) {
            AudioFrame sub;
            sub.session_id   = frame.session_id;
            sub.trace_id     = frame.trace_id;
            sub.sequence_num = frame.sequence_num;
            sub.sample_rate  = frame.sample_rate;
            sub.channels     = frame.channels;
            sub.codec        = frame.codec;
            sub.direction    = frame.direction;
            const size_t end = std::min(off + frame_bytes, total);
            sub.is_final     = frame.is_final && end == total;  // only last sub-frame
            sub.payload.assign(frame.payload.begin() + static_cast<std::ptrdiff_t>(off),
                               frame.payload.begin() + static_cast<std::ptrdiff_t>(end));
            media_->push_outbound(std::move(sub));
        }
    };

    cbs.on_cancel_ack = [this](const std::string& /*session_id*/) {
        post([this] { if (fsm_) fsm_->on_cancel_complete(); });
    };

    // Applied once the goodbye TTS finishes playing (on_playback_finished).
    cbs.on_end_call = [this](const std::string& /*session_id*/, std::string reason,
                             uint32_t grace_period_ms) {
        log_.info("EndCall received reason={} grace_period_ms={}", reason, grace_period_ms);
        post([this, grace_period_ms] {
            pending_end_call_       = true;
            pending_goodbye_timeout_ = std::chrono::milliseconds{grace_period_ms};
        });
    };

    cbs.on_transfer_requested = [this](const std::string& /*session_id*/,
                                        std::string transfer_type,
                                        std::string destination,
                                        std::string reason,
                                        std::string transfer_id,
                                        std::string caller_id,
                                        std::string waiting_experience) {
        log_.info("TransferRequest received type={} destination={} reason={} transfer_id={} "
                 "caller_id={} waiting_experience={}",
                  transfer_type, destination, reason, transfer_id, caller_id, waiting_experience);
        post([this, transfer_type, destination, reason, transfer_id,
              caller_id, waiting_experience]() mutable {
            active_transfer_id_          = std::move(transfer_id);
            active_transfer_destination_ = destination;
            active_transfer_type_        = transfer_type;
            active_caller_id_          = caller_id.empty() ? ctx_.caller_did : caller_id;
            active_waiting_experience_ = waiting_experience;
            // Unknown transfer_type falls back to cold.
            active_transfer_ = (transfer_type == "warm") ? static_cast<ITransferCoordinator*>(&warm_transfer_)
                                                          : static_cast<ITransferCoordinator*>(&cold_transfer_);
            if (fsm_) fsm_->on_transfer_requested(std::move(destination), std::move(reason));
        });
    };

    cbs.on_conversation_finalized = [this](const std::string& /*session_id*/) {
        log_.info("ConversationFinalized received");
        post([this] {
            if (fsm_) fsm_->on_conversation_finalized();
        });
    };

    cbs.on_error = [this](const std::string& /*sid*/, std::string error, bool fatal) {
        log_.error("Transport error: {} fatal={}", error, fatal);
        if (fatal) terminate("transport_error");
    };

    transport_->set_callbacks(std::move(cbs));
}

void CallSession::wire_media_callbacks() {
    MediaSessionCallbacks cbs;

    // Callbacks run on the AudioWorker thread and post to the control thread.

    // Recognizing → STT; barge-in → barge_in_buffer_; otherwise → pre-roll.
    cbs.on_audio_frame = [this](AudioFrame frame) {
        if (fsm_ && fsm_->can_accept_audio()) {
            post([this, f = std::move(frame)]() mutable {
                if (!fsm_) return;
                if (fsm_->state() == CallFsmState::Recognizing) {
                    transport_->send_audio(std::move(f));
                } else if (barge_in_capture_) {
                    if (barge_in_buffer_.size() < kMaxBargeInFrames)
                        barge_in_buffer_.push_back(std::move(f));
                } else {
                    preroll_.push_back(std::move(f));
                    if (preroll_.size() > kPrerollFrames)
                        preroll_.pop_front();
                }
            });
        }
    };

    // Barge-in branches seed capture with the pre-roll.
    cbs.on_speech_started = [this](float energy_db) {
        post([this, energy_db] {
            if (!fsm_) return;
            const auto s = fsm_->state();
            if (s == CallFsmState::Speaking) {
                barge_in_capture_    = true;
                barge_in_buffer_.assign(std::make_move_iterator(preroll_.begin()),
                                        std::make_move_iterator(preroll_.end()));
                preroll_.clear();
                barge_in_energy_db_  = energy_db;
                pending_speech_ended_ = false;
                media_->cancel_playback();
                transport_->cancel_generation(ctx_.obs.session_id);
            } else if (s == CallFsmState::Thinking || s == CallFsmState::Synthesizing) {
                barge_in_capture_    = true;
                barge_in_buffer_.assign(std::make_move_iterator(preroll_.begin()),
                                        std::make_move_iterator(preroll_.end()));
                preroll_.clear();
                barge_in_energy_db_  = energy_db;
                pending_speech_ended_ = false;
                if (media_->is_playing()) media_->cancel_playback();
                transport_->cancel_generation(ctx_.obs.session_id);
                fsm_->on_early_barge_in();
            } else {
                if (media_->is_playing()) media_->cancel_playback();  // greeting
                fsm_->on_speech_started(energy_db);
            }
        });
    };

    cbs.on_speech_ended = [this](uint32_t duration_ms, float energy_db) {
        post([this, duration_ms, energy_db] {
            if (!fsm_) return;
            if (barge_in_capture_) {
                // Interjection ended mid-cancel — replay after the flush.
                pending_speech_ended_   = true;
                pending_se_duration_ms_ = duration_ms;
                pending_se_energy_db_   = energy_db;
                return;
            }
            fsm_->notify_speech_ended(duration_ms, energy_db);
        });
    };

    cbs.on_playback_finished = [this] {
        post([this] {
            if (!fsm_) return;
            const bool     end_call = pending_end_call_;
            const auto     timeout  = pending_goodbye_timeout_;
            pending_end_call_        = false;
            pending_goodbye_timeout_ = {};
            fsm_->on_playback_finished(false, end_call, timeout);
        });
    };

    cbs.on_playback_cancelled = [this] {
        post([this] {
            // Barge-in makes a pending end-call stale.
            pending_end_call_        = false;
            pending_goodbye_timeout_ = {};
            if (fsm_) fsm_->on_playback_finished(true);
        });
    };

    media_->set_callbacks(std::move(cbs));
}

void CallSession::wire_connection_callbacks() {
    connection_->set_on_binary([this](const uint8_t* data, size_t len) {
        push_inbound_audio(data, len);
    });

    // The only expected text frame (metadata) was consumed before this session existed.
    connection_->set_on_text([this](const std::string& msg) {
        on_text_message(msg);
    });
}

void CallSession::on_text_message(const std::string& msg) {
    log_.warn("Unexpected mid-call WS text frame (ignored): {}", msg);
}

void CallSession::on_fsm_state_changed(CallFsmState from, CallFsmState to,
                                        std::string_view trigger, double duration_ms)
{
    SessionStateChangedEvent ev;
    ev.hdr = EventHeader::from(ctx_.obs);
    ev.from_state       = from;
    ev.to_state         = to;
    ev.trigger          = std::string(trigger);
    ev.prev_duration_ms = duration_ms;

    dispatcher_.dispatch(std::move(ev));

    sm_.observe("session.state_duration_ms", duration_ms);
    sm_.increment(std::string("session.state.") + std::string(to_string(to)));

    // Pre-roll first so STT gets audio from before the VAD onset.
    if (to == CallFsmState::Recognizing && !preroll_.empty()) {
        for (auto& f : preroll_)
            transport_->send_audio(std::move(f));
        preroll_.clear();
    }

    // Cancel completed: re-enter Recognizing and forward the barge-in audio.
    if (from == CallFsmState::BargeIn && to == CallFsmState::Listening
        && barge_in_capture_ && fsm_) {
        barge_in_capture_ = false;

        const bool has_audio = !barge_in_buffer_.empty();
        if (has_audio || !pending_speech_ended_) {
            fsm_->on_speech_started(barge_in_energy_db_);  // Listening→Recognizing
            for (auto& f : barge_in_buffer_)
                transport_->send_audio(std::move(f));
            if (pending_speech_ended_)
                fsm_->notify_speech_ended(pending_se_duration_ms_,
                                          pending_se_energy_db_);
        }
        barge_in_buffer_.clear();
        pending_speech_ended_ = false;
    }
}

} // namespace voiceai
