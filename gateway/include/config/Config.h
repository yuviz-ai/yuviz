#pragma once

#include "media/EnergyVADConfig.h"
#include "media/SileroVADConfig.h"
#include "session/CallFsmTimerConfig.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

namespace voiceai {

struct WebSocketConfig {
    std::string host{"0.0.0.0"};
    uint16_t    port{8080};
    uint32_t    max_connections{1000};
    uint32_t    timeout_ms{30000};

    // Max wait for mod_audio_fork's metadata frame before routing to the default
    // tenant/agent. Real calls send it first, so this only bounds misbehaving clients.
    uint32_t    metadata_wait_ms{300};
};

struct LoggingConfig {
    std::string level{"info"};
    std::string file{};
    bool        console{true};
};

struct MediaConfig {
    uint32_t sample_rate{16'000};  // 16 kHz L16 from mod_audio_fork
    uint8_t  channels{1};
    uint32_t frame_ms{20};
    uint32_t ring_buffer_ms{500};
    uint32_t playback_max_frames{3000}; // 60s at 20ms/frame — matches playback_timeout
    std::string vad_engine{"silero"};   // "silero" | "energy"
    std::string vad_model_path{"models/silero_vad.onnx"};
};

struct WorkerConfig {
    uint32_t audio_worker_threads{4};  // AudioWorkerPool thread count
};

struct MetricsConfig {
    bool        enabled{false};
    uint16_t    port{9090};
    std::string path{"/metrics"};
};

struct ConversationTransportConfig {
    std::string type{"null"};      // "null" | "grpc"
    std::string endpoint{"localhost:50051"};
    uint32_t    connect_timeout_ms{5000};
};

// FreeSWITCH ESL for call-control commands the WebSocket path can't express.
// When disabled, agent hangup closes audio but the SIP leg lingers until the caller hangs up.
struct EslConfig {
    bool        enabled{false};
    std::string host{"127.0.0.1"};
    uint16_t    port{8021};
    std::string password;              // FREESWITCH_ESL_PASSWORD (or esl.password); required when enabled
    uint32_t    connect_timeout_ms{2000};

    // All dialed numbers go via sofia/external/sip:<dest>@<sip_proxy_host> because phones
    // register with Kamailio, not FreeSWITCH. Empty = transfers refused (sip_proxy_host_unset).
    std::string sip_proxy_host{};
    uint16_t    sip_proxy_port{5060};
};

// Config-plane cache. Any Redis failure degrades to defaults; it never rejects a call.
struct RedisConfig {
    bool        enabled{false};
    std::string host{"127.0.0.1"};
    uint16_t    port{6379};
    uint32_t    connect_timeout_ms{200};
    uint32_t    command_timeout_ms{100};
    uint32_t    pool_size{4};
};

struct GatewayConfig {
    WebSocketConfig             websocket;
    LoggingConfig               logging;
    ConversationTransportConfig conversation;  // AI service transport
    MediaConfig                 media;
    WorkerConfig                workers;
    MetricsConfig               metrics;
    EslConfig                   esl;
    RedisConfig                 redis;
};

// Limits on inbound and outbound queue depth.
struct BackpressureConfig {
    size_t max_inbound_queue_frames{25};    // ring buffer capacity (25 * 20ms = 500ms default)
    size_t max_outbound_queue_frames{3000}; // PlaybackQueue capacity (60s at 20ms/frame)
};

// Per-session config snapshot, bound once at call creation and never mutated.
struct TenantConfig {
    std::string tenant_id;

    uint32_t sample_rate{16'000};
    uint8_t  channels{1};
    uint32_t frame_ms{20};

    std::string                 vad_engine{"silero"};  // "silero" | "energy"
    EnergyVADConfig             vad;      // fallback engine
    SileroVADConfig             silero;
    CallFsmTimerConfig          timers;
    ConversationTransportConfig transport;
    BackpressureConfig          backpressure;

    // Build a TenantConfig from GatewayConfig process-level defaults.
    [[nodiscard]] static TenantConfig from_default(const GatewayConfig& cfg) noexcept;

    // Overlays Redis `tenant:{tenant_id}` onto from_default(); any failure falls back to
    // from_default(cfg) in full. logger only surfaces validation warnings.
    [[nodiscard]] static TenantConfig from_redis(
        class RedisClient&  redis,
        const std::string&  tenant_id,
        const GatewayConfig& cfg,
        class Logger*        logger = nullptr) noexcept;
};

// DID → tenant/agent routing. Any lookup failure degrades to {"default", "default"};
// it never rejects a call.
struct PhoneRoute {
    std::string tenant_slug{"default"};
    std::string agent_slug{"default"};
    // Agent config_version; 0 when absent. Observability only.
    uint32_t    version{0};

    // Looks up `did:{did}` in Redis (written by services/config/phone_numbers.py).
    [[nodiscard]] static PhoneRoute from_redis(
        class RedisClient& redis, const std::string& did) noexcept;
};

// DID/ANI/direction from mod_audio_fork's first WS text frame.
// Missing or malformed input degrades to {"", "", "inbound"}; never throws.
struct CallMetadata {
    std::string did;              // called number — feeds PhoneRoute::from_redis()
    std::string ani;              // calling number — feeds SessionContext::caller_did
    std::string direction{"inbound"};

    // raw is nullopt when no frame arrived in time.
    [[nodiscard]] static CallMetadata parse(
        const std::optional<std::string>& raw) noexcept;
};

class Config {
public:
    Config() = default;
    ~Config() = default;

    Config(const Config&)            = delete;
    Config& operator=(const Config&) = delete;

    void load(const std::string& path);

    [[nodiscard]] const GatewayConfig&               gateway()      const noexcept { return config_; }
    [[nodiscard]] const WebSocketConfig&             websocket()    const noexcept { return config_.websocket; }
    [[nodiscard]] const LoggingConfig&               logging()      const noexcept { return config_.logging; }
    [[nodiscard]] const ConversationTransportConfig& conversation() const noexcept { return config_.conversation; }
    [[nodiscard]] const MediaConfig&                 media()        const noexcept { return config_.media; }
    [[nodiscard]] const MetricsConfig&               metrics()      const noexcept { return config_.metrics; }
    [[nodiscard]] const EslConfig&                   esl()          const noexcept { return config_.esl; }

private:
    GatewayConfig config_;
};

} // namespace voiceai
