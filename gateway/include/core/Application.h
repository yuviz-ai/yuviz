#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "common/SystemClock.h"
#include "config/Config.h"
#include "config/RedisClient.h"
#include "logging/Logger.h"
#include "telephony/EslClient.h"
#include "telephony/EslEventListener.h"
#include "telephony/TransferCorrelator.h"
#include "transport/ConversationTransportFactory.h"
#include "utils/ThreadPool.h"

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>

namespace voiceai {

class AudioWorkerPool;
class IComponent;
class IDispatcher;
class IMetrics;
class IWebSocketServer;
class SessionManager;
class TimerService;

// Single owner of all subsystems.
//
// Startup order:  Config → Logger → Metrics → Dispatcher
//                 → AudioWorkerPool → TimerService
//                 → ConversationTransportFactory (providers registered)
//                 → WebSocketServer
// Shutdown order: WebSocketServer → sessions cleared → TimerService
//                 → AudioWorkerPool → Dispatcher → Metrics
class Application : private NonCopyable, private NonMovable {
public:
    explicit Application(std::string config_path);
    ~Application();

    int  run();
    void shutdown();

    // Lets main.cpp register "grpc" without linking gRPC into gateway_lib.
    ConversationTransportFactory& transport_factory() noexcept {
        return transport_factory_;
    }

private:
    static void        signal_handler(int sig) noexcept;
    static Application* s_active_;   // for signal_handler; POSIX one-per-process

    void setup_signals();
    void initialize();
    void teardown();
    void wire_websocket_handlers();

    [[nodiscard]] size_t session_count() const;

    std::string config_path_;

    std::shared_ptr<const GatewayConfig> config_data_;
    std::unique_ptr<Logger>              logger_;
    SystemClock                          clock_;

    // ── Control-plane components ─────────────────────────────────────────────
    std::unique_ptr<IMetrics>         metrics_;
    std::unique_ptr<IDispatcher>      dispatcher_;
    std::unique_ptr<IWebSocketServer> ws_server_;

    // ── Data-plane components ────────────────────────────────────────────────
    std::unique_ptr<AudioWorkerPool>  audio_worker_pool_;
    std::unique_ptr<TimerService>     timer_service_;

    // ── Telephony control ────────────────────────────────────────────────────
    std::unique_ptr<EslClient>        esl_client_;

    // Declared before esl_event_listener_/session_manager_ so they outlive both.
    // transfer_correlator_: channel/leg uuids; job_correlator_: bgapi Job-UUIDs.
    TransferCorrelator                transfer_correlator_;
    TransferCorrelator                job_correlator_;

    // Separate ESL connection for CHANNEL_* events (see EslEventListener).
    std::unique_ptr<EslEventListener> esl_event_listener_;

    // ── Config-plane cache ───────────────────────────────────────────────────
    std::unique_ptr<RedisClient>      redis_client_;

    // Runs blocking Redis lookups + session creation off the shared lws thread.
    // Must be drained in teardown() before redis_client_ and session_manager_ die.
    std::unique_ptr<ThreadPool>       config_resolver_pool_;

    // ~CallSession() can block (thread joins); keep it off the lws thread and
    // separate from setup so teardown never queues behind new calls.
    std::unique_ptr<ThreadPool>       session_cleanup_pool_;

    // ── Transport factory ────────────────────────────────────────────────────
    ConversationTransportFactory      transport_factory_;

    // ── Session manager ──────────────────────────────────────────────────────
    std::unique_ptr<SessionManager>   session_manager_;

    std::atomic<bool>       running_{false};
    std::atomic<bool>       shutdown_requested_{false};
    std::mutex              shutdown_mutex_;
    std::condition_variable shutdown_cv_;
};

} // namespace voiceai
