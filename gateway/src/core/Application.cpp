#include "core/Application.h"
#include "core/PendingMetadata.h"
#include "config/Config.h"
#include "dispatcher/Dispatcher.h"
#include "media/AudioWorkerPool.h"
#include "metrics/Metrics.h"
#include "session/CallSessionFactory.h"
#include "session/SessionContext.h"
#include "session/SessionManager.h"
#include "timer/TimerService.h"
#include "transport/NullConversationTransport.h"
#include "websocket/WebSocketServer.h"

#include <chrono>
#include <condition_variable>
#include <csignal>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string_view>

namespace voiceai {

// ── Static member ─────────────────────────────────────────────────────────────

Application* Application::s_active_ = nullptr;

// ── Signal handler ────────────────────────────────────────────────────────────

void Application::signal_handler(int /*sig*/) noexcept {
    // Only touch the atomic — cv::notify_all() is not async-signal-safe (POSIX).
    // run() uses wait_for() with a 1s timeout so it wakes within 1 second.
    if (s_active_)
        s_active_->shutdown_requested_.store(true, std::memory_order_relaxed);
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

Application::Application(std::string config_path)
    : config_path_(std::move(config_path))
{
    s_active_ = this;
}

Application::~Application() {
    if (s_active_ == this) s_active_ = nullptr;
    teardown();
}

int Application::run() {
    try {
        setup_signals();
        initialize();
    } catch (const std::exception& e) {
        if (logger_) logger_->critical("Initialization failed: {}", e.what());
        return 1;
    }

    logger_->info("Voice AI Gateway running — awaiting connections");
    running_.store(true);

    {
        std::unique_lock lock{shutdown_mutex_};
        // Use wait_for so that a signal-handler-only store (no notify_all) wakes
        // within 1 s.  Programmatic shutdown() still calls notify_all() for
        // immediate wakeup in tests and controlled teardowns.
        while (!shutdown_requested_.load(std::memory_order_relaxed))
            shutdown_cv_.wait_for(lock, std::chrono::seconds{1});
    }

    logger_->info("Shutdown signal received");
    teardown();
    return 0;
}

void Application::shutdown() {
    shutdown_requested_.store(true, std::memory_order_relaxed);
    shutdown_cv_.notify_all();
}

void Application::setup_signals() {
    std::signal(SIGINT,  signal_handler);
    std::signal(SIGTERM, signal_handler);
}

void Application::initialize() {
    Config cfg;
    cfg.load(config_path_);
    config_data_ = std::make_shared<const GatewayConfig>(cfg.gateway());

    const auto& lc = config_data_->logging;
    logger_ = std::make_unique<Logger>("gateway", lc.file, lc.console);
    logger_->info("Configuration loaded from '{}'", config_path_);

    // ── Control-plane components ─────────────────────────────────────────────
    metrics_    = std::make_unique<Metrics>(*logger_);
    dispatcher_ = std::make_unique<Dispatcher>(*metrics_, *logger_);

    if (!metrics_->initialize() || !metrics_->start())
        throw std::runtime_error("Metrics failed to start");
    if (!dispatcher_->initialize() || !dispatcher_->start())
        throw std::runtime_error("Dispatcher failed to start");

    // ── Data-plane components ────────────────────────────────────────────────
    audio_worker_pool_ = std::make_unique<AudioWorkerPool>(
        config_data_->workers.audio_worker_threads,
        config_data_->media.frame_ms,
        *logger_);
    timer_service_ = std::make_unique<TimerService>(clock_, *logger_);

    if (!audio_worker_pool_->start())
        throw std::runtime_error("AudioWorkerPool failed to start");
    if (!timer_service_->start())
        throw std::runtime_error("TimerService failed to start");

    // ── Telephony control ────────────────────────────────────────────────────
    // Connects lazily on first hangup() call, not here — no-op entirely when
    // config_data_->esl.enabled is false (the default).
    esl_client_ = std::make_unique<EslClient>(config_data_->esl, *logger_);

    // ── Config-plane cache ────────────────────────────────────────────────────
    // Connects lazily on first get() call, not here — no-op entirely when
    // config_data_->redis.enabled is false (the default).
    redis_client_ = std::make_unique<RedisClient>(config_data_->redis, *logger_);

    // 2 threads: this only ever does one blocking Redis GET per new call
    // setup, not sustained work — sized for "never let a burst of new calls
    // queue behind one slow lookup", not for throughput.
    config_resolver_pool_ = std::make_unique<ThreadPool>(2, "config-resolver");

    // 2 threads: session teardown is normally near-instant; sized for "a
    // couple of calls hanging up/transferring at once" not sustained load.
    session_cleanup_pool_ = std::make_unique<ThreadPool>(2, "session-cleanup");

    // ── Transport factory ────────────────────────────────────────────────────
    // Register built-in providers.  "grpc" is registered by main.cpp after
    // construction (see transport_factory() accessor) so that GrpcConversation-
    // Transport does not need to be linked into gateway_lib (which is also
    // linked by the test binary that has no gRPC symbols).
    transport_factory_.register_provider("null", [](Logger& lg) {
        return std::make_unique<NullConversationTransport>(lg);
    });

    // ── Session manager ──────────────────────────────────────────────────────
    // ConversationTransportFactory is injected by reference; it must outlive
    // session_manager_ (guaranteed: transport_factory_ is a member of Application
    // and outlives session_manager_).
    auto call_session_factory = std::make_unique<CallSessionFactory>(
        transport_factory_,
        *audio_worker_pool_,
        *timer_service_,
        *dispatcher_,
        *metrics_,
        clock_,
        *logger_,
        *esl_client_,
        transfer_correlator_,
        job_correlator_);
    session_manager_ = std::make_unique<SessionManager>(
        std::move(call_session_factory), *logger_);

    if (!session_manager_->initialize() || !session_manager_->start())
        throw std::runtime_error("SessionManager failed to start");

    // Real-time caller-hangup detection — closes the gap between a caller
    // actually hanging up and the Gateway noticing (previously only
    // no_speech_timeout, up to a minute-plus later, or a WebSocket close
    // mod_audio_fork may send late). Started after session_manager_ since
    // its callback calls straight into it; degrades to a no-op when
    // esl.enabled is false, same as esl_client_.
    esl_event_listener_ = std::make_unique<EslEventListener>(
        config_data_->esl, *logger_,
        [this](const std::string& call_id) {
            session_manager_->terminate_by_call_id(call_id, "caller_hangup");
        },
        [this](const std::string& call_id, const std::string& digit) {
            session_manager_->push_dtmf_to_call(call_id, digit);
        },
        transfer_correlator_, job_correlator_);
    if (!esl_event_listener_->start())
        throw std::runtime_error("EslEventListener failed to start");

    // ── WebSocket server ─────────────────────────────────────────────────────
    ws_server_ = std::make_unique<WebSocketServer>(config_data_->websocket, *logger_);

    if (!ws_server_->initialize() || !ws_server_->start())
        throw std::runtime_error("WebSocketServer failed to start");

    wire_websocket_handlers();
}

void Application::wire_websocket_handlers() {
    ws_server_->set_on_connect([this](std::shared_ptr<IWebSocketConnection> conn) {
        if (session_manager_->active_count() >= config_data_->websocket.max_connections) {
            logger_->warn("Max connections ({}) reached — rejecting sid={}",
                          config_data_->websocket.max_connections, conn->id());
            conn->close();
            return;
        }

        const std::string sid  = conn->id();
        const std::string& wsp = conn->path();  // always "/voice/<uuid>" now

        // call_id is the FreeSWITCH channel UUID: the Lua dialplan script
        // passes it verbatim as the URL segment mod_audio_fork connects to
        // (see start_voice_ai.lua), so it is known and correct from the
        // first byte of the connection — unlike DID/ANI/direction, which
        // arrive in the metadata text frame below, call_id never needs to
        // wait for anything.
        static constexpr std::string_view kPrefix = "/voice/";
        std::string call_id;
        if (wsp.size() > kPrefix.size() && wsp.compare(0, kPrefix.size(), kPrefix) == 0)
            call_id = wsp.substr(kPrefix.size());

        // Rendezvous with the metadata text frame mod_audio_fork sends
        // before any audio (see CallMetadata's doc comment in Config.h).
        // Both handlers below run on this same lws thread whenever a frame
        // arrives for this connection — neither may block.
        auto pending = std::make_shared<PendingMetadata>();

        conn->set_on_text([pending](const std::string& msg) {
            pending->fulfill_with_text(msg);
        });

        conn->set_on_close([pending] {
            pending->fulfill_with_close();
        });

        // The bounded wait below, PhoneRoute::from_redis(), and
        // TenantConfig::from_redis() all either block or take bounded time
        // off this thread. Resolving them and creating the session happens
        // on config_resolver_pool_, never on this thread — this is the
        // *shared* libwebsockets service thread that also pumps I/O for
        // every other live call, so blocking here would stall their audio,
        // not just delay this one connection's setup. See
        // config_resolver_pool_'s declaration in Application.h.
        config_resolver_pool_->submit(
            [this, sid, call_id, pending, conn = std::move(conn)]() mutable {
                const auto meta_json = pending->wait_for(
                    std::chrono::milliseconds{config_data_->websocket.metadata_wait_ms});

                // Re-check after the wait: the connection may have closed
                // while we were waiting (on_close already fulfilled
                // pending, or the timeout raced it) — never construct a
                // session on a dead connection.
                if (!conn->is_open()) {
                    logger_->info("Connection closed before session setup sid={}", sid);
                    return;
                }

                try {
                    const CallMetadata md = CallMetadata::parse(meta_json);
                    logger_->info(
                        "Metadata frame resolved sid={} did={} ani={} direction={}",
                        sid, md.did, md.ani, md.direction);

                    // DID → tenant/agent routing (see database/schema.sql's
                    // phone_numbers table and services/config/phone_numbers.py).
                    // An unknown/empty DID or a Redis miss both resolve to
                    // {"default","default"} — the same tenant/agent every
                    // call used before this routing existed, never a
                    // rejected call.
                    const auto route = PhoneRoute::from_redis(*redis_client_, md.did);
                    logger_->info(
                        "Route resolved sid={} tenant={} agent={} version={}",
                        sid, route.tenant_slug, route.agent_slug, route.version);

                    SessionContext ctx;
                    // sid (WebSocketServer's connection handle) is a
                    // process-lifetime monotonic counter — unique only
                    // within one Gateway process's uptime, not across
                    // restarts. Using it as the persisted session_id (calls
                    // table PK) meant two calls landing on the same counter
                    // value after a Gateway restart collided: the second
                    // call's transcript rows silently appended onto the
                    // first's, and calls.turn_count/started_at went stale
                    // for good. call_id — the real FreeSWITCH channel
                    // UUID, already parsed above — is genuinely unique
                    // forever, so it's what session_id should actually be.
                    // sid remains in use purely as SessionManager's
                    // internal connection-map key (never persisted).
                    ctx.obs.session_id = call_id.empty() ? sid : call_id;
                    ctx.obs.tenant_id  = route.tenant_slug;
                    ctx.obs.call_id    = call_id;
                    ctx.script_id      = route.agent_slug;
                    ctx.called_did     = md.did;
                    ctx.caller_did     = md.ani;
                    ctx.direction      = md.direction;
                    ctx.tenant = std::make_shared<TenantConfig>(TenantConfig::from_redis(
                        *redis_client_, ctx.obs.tenant_id, *config_data_, logger_.get()));

                    session_manager_->create(sid, std::move(ctx), std::move(conn));

                    metrics_->increment("sessions.created");
                    metrics_->gauge("sessions.active",
                                     static_cast<double>(session_manager_->active_count()));
                } catch (const std::exception& e) {
                    // A discarded std::future would otherwise swallow this
                    // silently — surface it and drop the connection cleanly
                    // instead of leaving it half-set-up with no session.
                    logger_->error("Session setup failed sid={} err={}", sid, e.what());
                    conn->close();
                }
            });
    });

    ws_server_->set_on_disconnect([this](const std::string& sid) {
        // ~CallSession() runs on session_cleanup_pool_, not this lws
        // event-loop thread — see that pool's own declaration comment.
        // teardown() drains this pool before session_manager_/metrics_ are
        // destroyed, so a task still running at shutdown always finds
        // valid targets to call into.
        session_cleanup_pool_->submit([this, sid] {
            session_manager_->remove(sid);
            metrics_->increment("sessions.closed");
            metrics_->gauge("sessions.active", static_cast<double>(session_manager_->active_count()));
        });
    });
}

void Application::teardown() {
    if (!running_.exchange(false)) return;

    // 1. Stop accepting new connections so no new sessions can be created.
    if (ws_server_) { ws_server_->stop(); ws_server_->shutdown(); }

    // 1a. Stop before session_manager_ is torn down below — its callback
    //     calls straight into session_manager_, so it must not still be
    //     running once that pointer's target starts being destroyed.
    if (esl_event_listener_) esl_event_listener_->stop();

    // 1b. Drain any in-flight/queued session setup (see config_resolver_pool_'s
    //     declaration in Application.h) before touching anything its tasks
    //     reference — ws_server_ is already stopped, so no new tasks can be
    //     submitted past this point; shutdown() joins after the queue empties.
    if (config_resolver_pool_) config_resolver_pool_->shutdown();

    // 1c. Same reasoning, for session_cleanup_pool_ — ws_server_ being
    //     stopped above means set_on_disconnect can no longer submit new
    //     removal tasks, so this drains whatever's already in flight
    //     before session_manager_/metrics_ below are destroyed.
    if (session_cleanup_pool_) session_cleanup_pool_->shutdown();

    // 2. Destroy all live sessions via SessionManager.
    //    ~CallSession: stops control thread, unassigns from pool, cancels timers.
    //    Must happen before stopping the services those destructors call into.
    if (session_manager_) {
        session_manager_->stop();
        session_manager_->shutdown();
    }

    // 3. Stop data-plane services (all sessions have released their resources).
    if (timer_service_)    timer_service_->stop();
    if (audio_worker_pool_) audio_worker_pool_->stop();

    // 4. Stop control-plane components.
    if (dispatcher_) { dispatcher_->stop(); dispatcher_->shutdown(); }
    if (metrics_)    { metrics_->stop();    metrics_->shutdown();    }

    if (logger_) logger_->info("Teardown complete");
}

size_t Application::session_count() const {
    return session_manager_ ? session_manager_->active_count() : 0;
}

} // namespace voiceai
