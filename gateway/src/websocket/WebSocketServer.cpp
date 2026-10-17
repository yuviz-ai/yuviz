#include "websocket/WebSocketServer.h"
#include "common/ThreadUtils.h"
#include <atomic>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>

namespace voiceai {

namespace {

// lws per-session user data stores a pointer back to WebSocketServer.
struct PerSessionData {
    WebSocketServer* server{nullptr};
};

// Monotonic counter — produces a unique, non-address-based session ID so lws
// pointer reuse across connections never yields a duplicate ID.
std::string next_session_id() {
    static std::atomic<uint64_t> seq{1};
    const uint64_t id = seq.fetch_add(1, std::memory_order_relaxed);
    char buf[17];
    std::snprintf(buf, sizeof(buf), "%016" PRIx64, id);
    return buf;
}

} // namespace

WebSocketServer::WebSocketServer(const WebSocketConfig& config, Logger& logger)
    : config_(config)
    , logger_(logger)
{}

WebSocketServer::~WebSocketServer() {
    stop();
}

bool WebSocketServer::initialize() {
    return true;
}

bool WebSocketServer::start() {
    if (running_.exchange(true)) return true;

    // mod_audio_fork (drachtio) connects with Sec-WebSocket-Protocol: audio.drachtio.org.
    // "voice-ai" is kept as a fallback for the smoke test and direct clients.
    static lws_protocols protocols[] = {
        {
            "audio.drachtio.org",
            &WebSocketServer::lws_callback,
            sizeof(PerSessionData),
            65536,
            0, nullptr, 0
        },
        {
            "voice-ai",
            &WebSocketServer::lws_callback,
            sizeof(PerSessionData),
            65536,
            0, nullptr, 0
        },
        { nullptr, nullptr, 0, 0, 0, nullptr, 0 }
    };

    lws_context_creation_info info{};
    info.port      = static_cast<int>(config_.port);
    // Without iface lws binds every interface whatever websocket.host says.
    info.iface     = (config_.host.empty() || config_.host == "0.0.0.0") ? nullptr : config_.host.c_str();
    info.protocols = protocols;
    info.options   = LWS_SERVER_OPTION_HTTP_HEADERS_SECURITY_BEST_PRACTICES_ENFORCE;
    info.user      = this;

    context_ = lws_create_context(&info);
    if (!context_) {
        running_.store(false);
        throw std::runtime_error("lws_create_context failed");
    }

    service_thread_ = std::thread([this] { service_loop(); });
    logger_.info("WebSocketServer listening on {}:{}", config_.host, config_.port);
    return true;
}

void WebSocketServer::stop() {
    if (!running_.exchange(false)) return;

    // lws_service() ignores its timeout and can block in poll() for many seconds; wake it.
    if (context_) lws_cancel_service(context_);
    if (service_thread_.joinable()) service_thread_.join();

    if (context_) {
        lws_context_destroy(context_);
        context_ = nullptr;
    }
    logger_.info("WebSocketServer stopped");
}

void WebSocketServer::shutdown() {}

void WebSocketServer::set_on_connect(ConnectHandler handler) {
    on_connect_ = std::move(handler);
}

void WebSocketServer::set_on_disconnect(DisconnectHandler handler) {
    on_disconnect_ = std::move(handler);
}

void WebSocketServer::service_loop() {
    set_thread_name("WebSocket");

    while (running_.load(std::memory_order_acquire)) {
        lws_service(context_, 10);

        // Request writable callbacks for any connections that have data queued
        // by non-service threads (PlaybackDrain, control thread, etc.).
        // lws_callback_on_writable() MUST be called from the service thread.
        std::lock_guard lock{conn_mutex_};
        for (auto& [wsi, conn] : connections_) {
            if (conn->has_pending_writes())
                lws_callback_on_writable(wsi);
        }
    }
}

std::shared_ptr<WebSocketConnection> WebSocketServer::find_connection(struct lws* wsi) const {
    std::lock_guard lock{conn_mutex_};
    auto it = connections_.find(wsi);
    return (it != connections_.end()) ? it->second : nullptr;
}

void WebSocketServer::add_connection(struct lws* wsi,
                                     std::shared_ptr<WebSocketConnection> conn) {
    std::lock_guard lock{conn_mutex_};
    connections_.emplace(wsi, std::move(conn));
}

void WebSocketServer::remove_connection(struct lws* wsi) {
    std::lock_guard lock{conn_mutex_};
    connections_.erase(wsi);
}

int WebSocketServer::lws_callback(struct lws*              wsi,
                                  enum lws_callback_reasons reason,
                                  void*                    user,
                                  void*                    in,
                                  size_t                   len)
{
    auto* psd    = static_cast<PerSessionData*>(user);
    auto* server = psd ? psd->server : nullptr;

    if (!server) {
        auto* ctx_user = lws_context_user(lws_get_context(wsi));
        server = static_cast<WebSocketServer*>(ctx_user);
        if (psd) psd->server = server;
    }

    switch (reason) {
        case LWS_CALLBACK_ESTABLISHED: {
            const std::string conn_id = next_session_id();
            char path_buf[256] = {};
            lws_hdr_copy(wsi, path_buf, sizeof(path_buf), WSI_TOKEN_GET_URI);
            auto conn = std::make_shared<WebSocketConnection>(wsi, conn_id, std::string(path_buf));
            server->add_connection(wsi, conn);
            server->logger_.info("WebSocket connection established id={} path={}", conn_id, path_buf);
            if (server->on_connect_) server->on_connect_(conn);
            break;
        }
        case LWS_CALLBACK_RECEIVE: {
            auto conn = server->find_connection(wsi);
            if (conn) {
                const bool is_binary = lws_frame_is_binary(wsi) != 0;
                conn->handle_receive(static_cast<const uint8_t*>(in), len, is_binary);
            }
            break;
        }
        case LWS_CALLBACK_SERVER_WRITEABLE: {
            auto conn = server->find_connection(wsi);
            if (conn) conn->flush_pending_writes();
            break;
        }
        case LWS_CALLBACK_CLOSED: {
            auto conn = server->find_connection(wsi);
            if (conn) {
                const std::string closed_id = conn->id();
                server->logger_.info("WebSocket connection closed id={}", closed_id);
                conn->handle_close();
                if (server->on_disconnect_) server->on_disconnect_(closed_id);
                server->remove_connection(wsi);
            }
            break;
        }
        case LWS_CALLBACK_HTTP: {
            // Answered on the thread that moves call audio, so a stuck thread fails the probe.
            const bool health = in != nullptr && std::strcmp(static_cast<const char*>(in), "/health") == 0;
            if (lws_return_http_status(wsi, health ? HTTP_STATUS_OK : HTTP_STATUS_NOT_FOUND, health ? "ok" : nullptr))
                return -1;
            return lws_http_transaction_completed(wsi) ? -1 : 0;
        }
        default:
            break;
    }
    return 0;
}

} // namespace voiceai
