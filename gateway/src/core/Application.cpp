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
    // notify_all() isn't async-signal-safe; run() polls this flag every 1s.
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
        // Timed wait: the signal handler sets the flag without notifying.
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
    // Connects lazily.
    esl_client_ = std::make_unique<EslClient>(config_data_->esl, *logger_);

    // ── Config-plane cache ────────────────────────────────────────────────────
    // Connects lazily.
    redis_client_ = std::make_unique<RedisClient>(config_data_->redis, *logger_);

    config_resolver_pool_ = std::make_unique<ThreadPool>(2, "config-resolver");

    session_cleanup_pool_ = std::make_unique<ThreadPool>(2, "session-cleanup");

    // ── Transport factory ────────────────────────────────────────────────────
    // "grpc" is registered by main.cpp.
    transport_factory_.register_provider("null", [](Logger& lg) {
        return std::make_unique<NullConversationTransport>(lg);
    });

    // ── Session manager ──────────────────────────────────────────────────────
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

    // Started after session_manager_, which its callbacks call into.
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
        const std::string& wsp = conn->path();  // "/voice/<uuid>"

        // The URL segment is the FreeSWITCH channel UUID (set by start_voice_ai.lua).
        static constexpr std::string_view kPrefix = "/voice/";
        std::string call_id;
        if (wsp.size() > kPrefix.size() && wsp.compare(0, kPrefix.size(), kPrefix) == 0)
            call_id = wsp.substr(kPrefix.size());

        // These handlers run on the lws thread and must not block.
        auto pending = std::make_shared<PendingMetadata>();

        conn->set_on_text([pending](const std::string& msg) {
            pending->fulfill_with_text(msg);
        });

        conn->set_on_close([pending] {
            pending->fulfill_with_close();
        });

        // Blocking setup runs off the shared lws thread so other calls' audio never stalls.
        config_resolver_pool_->submit(
            [this, sid, call_id, pending, conn = std::move(conn)]() mutable {
                const auto meta_json = pending->wait_for(
                    std::chrono::milliseconds{config_data_->websocket.metadata_wait_ms});

                // The connection may have closed during the wait.
                if (!conn->is_open()) {
                    logger_->info("Connection closed before session setup sid={}", sid);
                    return;
                }

                try {
                    const CallMetadata md = CallMetadata::parse(meta_json);
                    logger_->info(
                        "Metadata frame resolved sid={} did={} ani={} direction={}",
                        sid, md.did, md.ani, md.direction);

                    const auto route = PhoneRoute::from_redis(*redis_client_, md.did);
                    logger_->info(
                        "Route resolved sid={} tenant={} agent={} version={}",
                        sid, route.tenant_slug, route.agent_slug, route.version);

                    SessionContext ctx;
                    // sid is a per-process counter that repeats across restarts, so the
                    // persisted session_id must be the channel UUID.
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
                    // The discarded future would swallow this silently.
                    logger_->error("Session setup failed sid={} err={}", sid, e.what());
                    conn->close();
                }
            });
    });

    ws_server_->set_on_disconnect([this](const std::string& sid) {
        // ~CallSession() can block, so keep it off the lws thread.
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

    // 1a. Its callbacks call into session_manager_.
    if (esl_event_listener_) esl_event_listener_->stop();

    // 1b. Drain pools whose tasks reference session_manager_/metrics_; no new
    //     tasks can arrive now that ws_server_ is stopped.
    if (config_resolver_pool_) config_resolver_pool_->shutdown();
    if (session_cleanup_pool_) session_cleanup_pool_->shutdown();

    // 2. Before stopping the services ~CallSession calls into.
    if (session_manager_) {
        session_manager_->stop();
        session_manager_->shutdown();
    }

    // 3. Data-plane services.
    if (timer_service_)    timer_service_->stop();
    if (audio_worker_pool_) audio_worker_pool_->stop();

    // 4. Control-plane components.
    if (dispatcher_) { dispatcher_->stop(); dispatcher_->shutdown(); }
    if (metrics_)    { metrics_->stop();    metrics_->shutdown();    }

    if (logger_) logger_->info("Teardown complete");
}

size_t Application::session_count() const {
    return session_manager_ ? session_manager_->active_count() : 0;
}

} // namespace voiceai
