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

    // Start control thread before assigning to the worker pool: any media
    // callbacks that fire immediately after assign() need the control queue ready.
    control_thread_ = std::thread([this] {
        set_thread_name("Ctrl-" + ctx_.obs.session_id.substr(0, 8));
        control_loop();
    });

    pool_.assign(media_.get());

    // Start playback drain before opening transport so TTS chunks that arrive
    // during the session_open handshake are consumed immediately.
    playback_drain_.start();
    transport_->start();

    // Kick off FSM through the control queue.
    post([this] { fsm_->on_session_start(); });
    transport_->open_session(ctx_);
}

CallSession::~CallSession() {
    // -1. Give an actively-resolving transfer coordinator a bounded chance
    //     to finish on its own thread before anything below touches it.
    //     Confirmed live (2026-07-20): warm transfer's stop_audio_fork()
    //     makes mod_audio_fork close its WebSocket connection as a side
    //     effect — well before the coordinator's own remaining steps
    //     (unhold, bridge(), finish()) complete on EslEventListener's
    //     thread — and that disconnect is exactly what triggers this
    //     destructor (see SessionManager::remove()). Without this wait,
    //     active_transfer_->shutdown() and transport_->stop() below could
    //     both run WHILE the coordinator was still mid-flight, silently
    //     discarding TransferCompleted before the Conversation Service
    //     ever received it — a fully successful transfer with no summary
    //     or transcript ever persisted, because finalize_session() never
    //     got a chance to run. Only safe to block here because destruction
    //     now happens on its own thread (see Application.cpp's
    //     set_on_disconnect — this was already flagged as a TODO before
    //     today: blocking the shared lws thread here would have stalled
    //     every other live call for the same duration.
    if (ITransferCoordinator* t = active_transfer_.load(std::memory_order_acquire)) {
        constexpr auto kMaxWait      = std::chrono::milliseconds{3000};
        constexpr auto kPollInterval = std::chrono::milliseconds{10};
        const auto deadline = std::chrono::steady_clock::now() + kMaxWait;
        while (t->state() == CoordinatorState::Active &&
               std::chrono::steady_clock::now() < deadline) {
            std::this_thread::sleep_for(kPollInterval);
        }
    }

    // 0. Shut down any in-flight transfer coordinator (crash fix, found
    //    live 2026-07-18, generalized — see docs/warm_transfer_
    //    architecture.md "Coordinator lifetime"): a coordinator's
    //    correlator/registry watch captures `this` and post()s from
    //    EslEventListener's thread. Even after the wait above, cover the
    //    case where the coordinator never resolves at all within the
    //    bound (or the caller hangs up before it starts) — a
    //    CHANNEL_HANGUP/CHANNEL_BRIDGE/CHANNEL_ANSWER that arrives later
    //    would otherwise fire that callback against a destroyed session —
    //    locking a destroyed mutex and aborting the whole process (libc++
    //    "mutex lock failed: Invalid argument"). active_transfer_->
    //    shutdown() is idempotent and guarantees no further callback fires
    //    after this returns — the primary safety layer. Declaration order
    //    (cold_transfer_/warm_transfer_ destroyed before esl_client_/
    //    transfer_correlator_ below, by virtue of being declared after
    //    them) is the second, independent layer — even a buggy/skipped
    //    shutdown() still can't produce a use-after-free via implicit
    //    destruction order alone.
    if (ITransferCoordinator* t = active_transfer_.load(std::memory_order_acquire)) {
        t->shutdown();
        active_transfer_.store(nullptr, std::memory_order_release);
    }

    // 1. Stop AudioWorker — no more media callbacks will be posted.
    pool_.unassign(media_.get());

    // 2. Stop transport — joins gRPC reader/writer threads so no transport
    //    callbacks can fire after this point.  Control thread is still alive
    //    to safely process any callbacks queued before the join.
    transport_->stop();

    // 3. Flush and stop the control thread.
    //    Cancel all timers AND close the FSM in a single item pushed before
    //    setting control_stopping_.  This eliminates the UAF window that
    //    existed when timer cancellation happened after join(): TimerService
    //    could pop a non-cancelled timer, execute its callback (which calls
    //    post()), and race with the control_queue_ destructor.
    {
        std::lock_guard lock{control_mutex_};
        control_queue_.push_back([this] {
            // Close FSM first: on_session_close() transitions to Closing and
            // schedules a new CloseTimeout timer (via on_enter(Closing)).
            // Cancel ALL timers afterwards so the CloseTimeout is also swept up.
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
    // timer_map_ is empty — cleared AFTER fsm_->on_session_close() so the
    // CloseTimeout it schedules is also cancelled.

    // 4. Stop playback drain.
    playback_drain_.stop();

    // 5. Safety no-op: stop() already called close_session(); this is idempotent.
    transport_->close_session(ctx_.obs.session_id);
}

void CallSession::push_inbound_audio(const uint8_t* data, size_t len) noexcept {
    // fsm_->can_accept_audio() reads std::atomic<CallFsmState> — safe from any thread.
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

    // STT transcript arrived — log it and rotate the provider_request_id so
    // every subsequent log line is tagged with this turn's trace context.
    h.on_stt_final = [this](std::string text, float confidence) {
        log_.info("stt_final text=\"{}\" confidence={:.2f}", text, confidence);
        log_.set_provider_request_id(ctx_.obs.trace_id + "-stt");
    };

    // Playback complete — notify the ConversationService so it starts the next
    // turn, and reset provider_request_id.  Fires identically for Speaking→
    // Listening and Speaking→WaitingForHangup: Python only needs to know
    // playback ended, not that a hangup grace period may now be running.
    h.on_playback_finished = [this](bool interrupted) {
        transport_->send_playback_finished(ctx_.obs.session_id, interrupted);
        log_.set_provider_request_id("");
    };

    // Notify the ConversationService that the utterance has ended.  The service
    // accumulates audio chunks and runs STT when it receives this signal, then
    // responds with SttResult → TtsStarted → TtsChunk(s).
    h.on_speech_ended = [this](uint32_t duration_ms, float energy_db) {
        transport_->send_speech_ended(ctx_.obs.session_id, duration_ms, energy_db);
    };

    // Phase 6 (warm transfer refactor — see
    // docs/warm_transfer_architecture.md): this handler now only owns the
    // logic genuinely shared by every transfer strategy (empty-destination
    // defense, the TransferInitiated notification, attempt bookkeeping) and
    // delegates the actual ESL/telephony work to active_transfer_ (selected
    // in cbs.on_transfer_requested below, by transfer_type). Cold's uuid_
    // transfer sequencing itself now lives in ColdTransferCoordinator —
    // moved verbatim, not rewritten.
    //
    // Fires *before* CallFSM::on_transfer_requested transitions to
    // Transferring (see that method — the handler runs, then the
    // transition happens). post() defers this whole body to run after the
    // current control-thread task (this FSM trigger call, transition
    // included) finishes — by then the FSM is actually in Transferring, so
    // fsm_->on_transfer_completed() (called from active_transfer_'s
    // callback, or directly below for an immediately-rejected command) is
    // safe to call.
    h.on_transfer_requested = [this](std::string queue_id, std::string reason) {
        log_.info("Transfer requested destination={} reason={} transfer_id={}",
                  queue_id, reason, active_transfer_id_);
        post([this, destination = std::move(queue_id), reason = std::move(reason)]() mutable {
            const std::string call_id = ctx_.obs.call_id;
            transfer_started_at_ = clock_.now();
            sm_.increment("transfers.attempted");

            // Defense in depth (the Conversation Service refuses to send an
            // empty destination — see servicer.py): resolve the attempt as
            // failed immediately rather than handing a coordinator a command
            // it will reject anyway, keeping one code path for the outcome.
            // Shared across strategies so neither coordinator needs to
            // duplicate this check.
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

            // Phase 5B: tell the Conversation Service a transfer is starting
            // *before* anything else — this is its one chance to react
            // (drive its own FSM, log, observe — and, per
            // docs/warm_transfer_architecture.md §7, eventually where
            // summary generation starts) before the AI session gets closed,
            // which may happen as soon as the outcome is confirmed (see
            // h.on_transfer_completed below). Sent identically for both
            // strategies, at this same earliest point — never delegated to
            // active_transfer_, which doesn't need to know this message
            // exists (see ITransferCoordinator.h).
            transport_->send_transfer_initiated(ctx_.obs.session_id, active_transfer_type_,
                                                destination, reason, active_transfer_id_);

            // Default cause if nothing below ever overwrites it — i.e.
            // CallFSM's own TransferTimeout fired with neither a real
            // outcome nor an immediate command rejection ever having
            // resolved things first. See h.on_transfer_completed, which
            // reads this once.
            pending_transfer_detail_ = "transfer_timeout";

            TransferCoordinatorContext tctx{call_id, destination, reason, active_transfer_id_,
                                           active_caller_id_, active_waiting_experience_};
            TransferCoordinatorCallbacks tcbs;
            // Set directly (not via post()) — must be visible before the
            // WebSocket disconnect this same command can trigger has any
            // chance to arrive; see this flag's own declaration comment and
            // TransferCoordinatorCallbacks::on_media_handoff's.
            tcbs.on_media_handoff = [this] {
                sip_leg_handed_off_.store(true, std::memory_order_relaxed);
            };
            tcbs.on_transfer_completed =
                [this](bool success, std::string dest, std::string detail) {
                    // May fire on EslEventListener's background thread (the
                    // normal, event-driven case) or synchronously from
                    // within active_transfer_->start() itself (an
                    // immediately-rejected command) — post() unconditionally
                    // so h.on_transfer_completed always runs on the control
                    // thread, and never re-enters the coordinator's own
                    // call stack in the synchronous case either.
                    post([this, success, dest = std::move(dest), detail = std::move(detail)]() mutable {
                        pending_transfer_detail_ = std::move(detail);
                        if (fsm_) fsm_->on_transfer_completed(success, std::move(dest));
                    });
                };
            active_transfer_.load(std::memory_order_acquire)->start(std::move(tctx), std::move(tcbs));
            // else: pending — wait for active_transfer_'s callback
            // (event-driven) or CallFSM's own TransferTimeout, whichever
            // comes first; either path ends at h.on_transfer_completed below.
        });
    };

    // The one place a transfer attempt (however it was resolved: a real
    // outcome, an immediately-rejected command, or CallFSM's own
    // TransferTimeout firing with no event ever having arrived) finishes.
    // Sends the Phase 5B TransferCompleted/TransferFailed notification and
    // shuts down active_transfer_ either way — see ITransferCoordinator.h's
    // lifecycle contract and docs/warm_transfer_architecture.md's
    // "Coordinator lifetime" section (this is the *normal-completion* call
    // to shutdown(); ~CallSession() calls it again for the teardown-mid-
    // transfer case — both paths are safe because shutdown() is idempotent).
    //
    // Phase 5D: on success, does NOT close the AI session here — CallFSM
    // moves to Finalizing instead of Closing (see do_transfer_completed_),
    // and close_session() is deferred to h.on_conversation_finalized
    // below, once the Conversation Service confirms its own post-call
    // cleanup is done (or FinalizingTimeout gives up waiting).
    //
    // On failure, this ALSO must not close the session: CallFSM moves to
    // Thinking (see do_transfer_completed_'s own comment), because the
    // Conversation Service's on_transfer_failed() is about to stream back
    // a real apology (an actual LLM call, observed live to take several
    // seconds) over this same stream. An earlier version called
    // close_session() here immediately after send_transfer_failed() —
    // confirmed live to race WritesDone()/TryCancel() against the
    // apology's own generation, killing it before a single TTS chunk
    // could arrive: the caller heard dead air instead of an apology.
    h.on_transfer_completed = [this](bool success, std::string destination) {
        // CallFSM's own TransferTimeout path resolves with no destination of
        // its own (see FsmTimerType::TransferTimeout) — fall back to the one
        // recorded when the attempt started so the outcome log names it.
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

    // Phase 5D: fires once, when Finalizing exits — either a real
    // ConversationFinalized message from the Conversation Service, or
    // FinalizingTimeout giving up on waiting for one. This is where the
    // AI session actually closes for a successfully-transferred call.
    // close_session() is idempotent (see GrpcConversationTransport), so
    // it's safe even if the transport was somehow already closed.
    h.on_conversation_finalized = [this] {
        log_.info("Conversation finalized — closing AI session");
        transport_->close_session(ctx_.obs.session_id);
    };

    // Fires for every path into Closing — external hangup (terminate()) and
    // internal FSM timers alike.  connection_->close() must live here (not
    // just in terminate()): a timer-triggered close previously only flipped
    // internal FSM state, leaving the WebSocket/RTP session open forever —
    // the caller stayed connected to an unresponsive line until they hung up
    // themselves.  close() is idempotent, so it's harmless if terminate()
    // already triggered it via this same path.  esl_client_.hangup() issues
    // the actual SIP BYE, since closing the WebSocket only stops the audio
    // stream; it's a safe no-op when ESL is unconfigured or ctx_.obs.call_id
    // is still empty (not yet populated from the WS path or mod_audio_fork).
    h.on_session_close = [this](std::string reason) {
        log_.info("Session close reason={}", reason);
        connection_->close();
        // A transfer attempt already handed this channel off to something
        // outside the Gateway's control (see sip_leg_handed_off_'s own
        // comment) — issuing uuid_kill here would disconnect a customer
        // who may already be live with a real human, not "this call's own"
        // SIP leg to clean up. The WebSocket closing on its own (mod_
        // audio_fork detaching) is exactly what a successful handoff looks
        // like, not something to react to as a hangup.
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

    // STT transcript ready — advance FSM Recognizing → Thinking.
    cbs.on_stt_result = [this](std::string text, float confidence) {
        post([this, t = std::move(text), confidence]() mutable {
            if (fsm_) fsm_->on_stt_final(std::move(t), confidence);
        });
    };

    // TTS is about to start — advance FSM Thinking → Synthesizing.
    // Also arm the first-chunk flag so the first on_tts_chunk transitions
    // Synthesizing → Speaking; subsequent chunks skip the redundant post.
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

    // Agent decided the call is over.  Don't close anything yet — the
    // goodbye TTS for this turn is still streaming/queued; wait for it to
    // actually finish playing (see wire_media_callbacks' cbs.on_playback_
    // finished) so the caller hears the whole thing before the line drops,
    // then gets a short grace window to speak up (WaitingForHangup) before
    // the gateway actually hangs up.
    cbs.on_end_call = [this](const std::string& /*session_id*/, std::string reason,
                             uint32_t grace_period_ms) {
        log_.info("EndCall received reason={} grace_period_ms={}", reason, grace_period_ms);
        post([this, grace_period_ms] {
            pending_end_call_       = true;
            pending_goodbye_timeout_ = std::chrono::milliseconds{grace_period_ms};
        });
    };

    // ConversationService requested a hand-off to a human: drive CallFSM
    // into Transferring, whose handler (wire_fsm_handlers()'
    // h.on_transfer_requested above) notifies the Conversation Service and
    // executes the transfer over ESL (uuid_transfer), then waits on
    // CHANNEL_BRIDGE/CHANNEL_HANGUP correlation for the outcome.
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
            // Empty means the Conversation Service wants the caller's own
            // ANI — resolved here, once, rather than in WarmTransferCoordinator
            // (see active_caller_id_'s own declaration comment).
            active_caller_id_          = caller_id.empty() ? ctx_.caller_did : caller_id;
            active_waiting_experience_ = waiting_experience;
            // Selects which coordinator owns this attempt — the only place
            // in CallSession that branches on transfer type at all (see
            // ITransferCoordinator.h: everything downstream of this line
            // goes through active_transfer_ generically). Both value
            // members already exist; nothing here allocates. An unknown
            // transfer_type value falls back to cold rather than crashing
            // or silently doing nothing — same degrade-safely posture as
            // the rest of this codebase's config handling.
            active_transfer_ = (transfer_type == "warm") ? static_cast<ITransferCoordinator*>(&warm_transfer_)
                                                          : static_cast<ITransferCoordinator*>(&cold_transfer_);
            if (fsm_) fsm_->on_transfer_requested(std::move(destination), std::move(reason));
        });
    };

    // Phase 5D: the Conversation Service confirms its own post-call cleanup
    // is done — drives CallFSM out of Finalizing (see
    // CallFSM::on_conversation_finalized and wire_fsm_handlers()'
    // h.on_conversation_finalized, which actually closes the AI session).
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

    // All callbacks run on the AudioWorker thread and post to the control queue
    // so the FSM is only ever driven from the control thread.

    // Audio routing: Recognizing → forward to STT; barge-in capture → hold in
    // barge_in_buffer_ until the cancel completes; otherwise → pre-roll window
    // so the next utterance keeps its first word.
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

    // Speech onset: barge-in from Speaking (cancel playback) or from
    // Thinking/Synthesizing (cancel pipeline before audio exists); otherwise
    // a normal Listening→Recognizing turn start.  Barge-in branches start
    // capture seeded with the pre-roll.
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
            // Consume pending_end_call_/pending_goodbye_timeout_ here, at the
            // point the decision is actually made: Speaking exits to
            // WaitingForHangup instead of Listening when the agent's goodbye
            // played out in full.
            const bool     end_call = pending_end_call_;
            const auto     timeout  = pending_goodbye_timeout_;
            pending_end_call_        = false;
            pending_goodbye_timeout_ = {};
            fsm_->on_playback_finished(false, end_call, timeout);
        });
    };

    cbs.on_playback_cancelled = [this] {
        post([this] {
            // Caller barged in during what would have been the agent's
            // goodbye — they're still talking, so the pending hangup is stale.
            pending_end_call_        = false;
            pending_goodbye_timeout_ = {};
            if (fsm_) fsm_->on_playback_finished(true);
        });
    };

    media_->set_callbacks(std::move(cbs));
}

void CallSession::wire_connection_callbacks() {
    // Route inbound binary (L16 PCM) frames from the WebSocket into the
    // MediaSession ring buffer.  push_inbound_audio() reads can_accept_audio()
    // atomically — safe to call from the lws service thread.
    connection_->set_on_binary([this](const uint8_t* data, size_t len) {
        push_inbound_audio(data, len);
    });

    // mod_audio_fork's `metadata` argument (see audio_pipe.cpp) is fixed at
    // `uuid_audio_fork start` time and sent exactly once, before any binary
    // frame — Application.cpp's on_connect handler already consumes that
    // frame (see CallMetadata::parse()) to build this CallSession's
    // SessionContext. By the time a CallSession exists, mod_audio_fork has
    // no mechanism left to send another text frame for this connection.
    connection_->set_on_text([this](const std::string& msg) {
        on_text_message(msg);
    });
}

void CallSession::on_text_message(const std::string& msg) {
    // This handler exists only to make an unexpected mid-call text frame
    // visible (a future mod_audio_fork/Lua change, a misbehaving client)
    // rather than silently doing nothing — nothing is expected to arrive
    // here today.
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

    // Entering Recognizing: forward the pre-roll window first so STT sees the
    // utterance from before the VAD onset threshold was crossed.
    if (to == CallFsmState::Recognizing && !preroll_.empty()) {
        for (auto& f : preroll_)
            transport_->send_audio(std::move(f));
        preroll_.clear();
    }

    // Cancel completed (CancelAck or BargeInWindow timer): re-enter Recognizing
    // and forward the audio captured during the barge-in.
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
