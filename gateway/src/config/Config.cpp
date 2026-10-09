#include "config/Config.h"
#include "config/RedisClient.h"
#include "logging/Logger.h"

#include <nlohmann/json.hpp>
#include <yaml-cpp/yaml.h>

#include <cstdlib>
#include <stdexcept>
#include <string>

namespace voiceai {

namespace {

const char* env_value(const char* name) {
    const char* v = std::getenv(name);
    return (v != nullptr && *v != '\0') ? v : nullptr;
}

uint16_t parse_port(const char* name, const char* v) {
    const std::string s(v);
    if (s.empty() || s.find_first_not_of("0123456789") != std::string::npos || s.size() > 5) {
        throw std::runtime_error(std::string(name) + " is not a port number: '" + s + "'");
    }
    const unsigned long port = std::stoul(s);
    if (port < 1 || port > 65535) {
        throw std::runtime_error(std::string(name) + " must be 1-65535, got " + s);
    }
    return static_cast<uint16_t>(port);
}

// Same variables Campaigns reads, so each value is set once in .env.
void apply_esl_env(EslConfig& esl) {
    if (const char* v = env_value("FREESWITCH_ESL_PASSWORD")) esl.password = v;
    if (const char* v = env_value("FREESWITCH_ESL_HOST"))     esl.host = v;
    if (const char* v = env_value("FREESWITCH_ESL_PORT"))     esl.port = parse_port("FREESWITCH_ESL_PORT", v);
    if (const char* v = env_value("SIP_PROXY_HOST"))          esl.sip_proxy_host = v;
    if (const char* v = env_value("SIP_PROXY_PORT"))          esl.sip_proxy_port = parse_port("SIP_PROXY_PORT", v);
    if (esl.enabled && esl.password.empty()) {
        throw std::runtime_error("esl.enabled but no ESL password: set FREESWITCH_ESL_PASSWORD in .env");
    }
}

// redis://host[:port][/0]. The Gateway uses db 0 and no auth, so anything else is refused.
void apply_redis_url(RedisConfig& redis, const std::string& url) {
    const std::string scheme = "redis://";
    if (url.rfind(scheme, 0) != 0) {
        throw std::runtime_error("REDIS_URL must start with redis://, got '" + url + "'");
    }
    std::string rest = url.substr(scheme.size());
    if (rest.find('@') != std::string::npos) {
        throw std::runtime_error("REDIS_URL: the Gateway does not support Redis auth");
    }
    std::string db;
    if (const auto slash = rest.find('/'); slash != std::string::npos) {
        db = rest.substr(slash + 1);
        rest = rest.substr(0, slash);
    }
    if (!db.empty() && db != "0") {
        throw std::runtime_error("REDIS_URL: the Gateway only uses db 0, got /" + db);
    }
    const auto colon = rest.rfind(':');
    const std::string host = colon == std::string::npos ? rest : rest.substr(0, colon);
    if (host.empty()) throw std::runtime_error("REDIS_URL has no host: '" + url + "'");
    redis.host = host;
    redis.port = colon == std::string::npos ? 6379 : parse_port("REDIS_URL port", rest.substr(colon + 1).c_str());
}

// .env owns every address and secret; gateway.yaml only tunes behaviour.
void apply_env(GatewayConfig& cfg) {
    apply_esl_env(cfg.esl);
    if (const char* v = env_value("CONVERSATION_SVC_TARGET")) cfg.conversation.endpoint = v;
    if (const char* v = env_value("REDIS_URL"))               apply_redis_url(cfg.redis, v);
    if (const char* v = env_value("GATEWAY_LISTEN_HOST"))     cfg.websocket.host = v;
}

}  // namespace

void Config::load(const std::string& path) {
    YAML::Node root;
    try {
        root = YAML::LoadFile(path);
    } catch (const YAML::Exception& e) {
        throw std::runtime_error("Failed to load config file '" + path + "': " + e.what());
    }

    const auto gw = root["gateway"];
    if (!gw) {
        apply_env(config_);
        return;
    }

    if (const auto ws = gw["websocket"]) {
        if (ws["host"])            config_.websocket.host            = ws["host"].as<std::string>();
        if (ws["port"])            config_.websocket.port            = ws["port"].as<uint16_t>();
        if (ws["max_connections"]) config_.websocket.max_connections = ws["max_connections"].as<uint32_t>();
        if (ws["timeout_ms"])      config_.websocket.timeout_ms      = ws["timeout_ms"].as<uint32_t>();
        if (ws["metadata_wait_ms"]) config_.websocket.metadata_wait_ms = ws["metadata_wait_ms"].as<uint32_t>();
    }

    if (const auto log = gw["logging"]) {
        if (log["level"])   config_.logging.level   = log["level"].as<std::string>();
        if (log["file"])    config_.logging.file    = log["file"].as<std::string>();
        if (log["console"]) config_.logging.console = log["console"].as<bool>();
    }

    if (const auto med = gw["media"]) {
        if (med["sample_rate"])    config_.media.sample_rate    = med["sample_rate"].as<uint32_t>();
        if (med["channels"])       config_.media.channels       = med["channels"].as<uint8_t>();
        if (med["frame_ms"])       config_.media.frame_ms       = med["frame_ms"].as<uint32_t>();
        if (med["ring_buffer_ms"]) config_.media.ring_buffer_ms = med["ring_buffer_ms"].as<uint32_t>();
        if (med["vad_engine"])     config_.media.vad_engine     = med["vad_engine"].as<std::string>();
        if (med["vad_model_path"]) config_.media.vad_model_path = med["vad_model_path"].as<std::string>();
    }

    if (const auto conv = gw["conversation"]) {
        if (conv["type"])               config_.conversation.type               = conv["type"].as<std::string>();
        if (conv["endpoint"])           config_.conversation.endpoint           = conv["endpoint"].as<std::string>();
        if (conv["connect_timeout_ms"]) config_.conversation.connect_timeout_ms = conv["connect_timeout_ms"].as<uint32_t>();
    }

    if (const auto met = gw["metrics"]) {
        if (met["enabled"]) config_.metrics.enabled = met["enabled"].as<bool>();
        if (met["port"])    config_.metrics.port    = met["port"].as<uint16_t>();
        if (met["path"])    config_.metrics.path    = met["path"].as<std::string>();
    }

    if (const auto esl = gw["esl"]) {
        if (esl["enabled"])            config_.esl.enabled            = esl["enabled"].as<bool>();
        if (esl["host"])               config_.esl.host               = esl["host"].as<std::string>();
        if (esl["port"])               config_.esl.port               = esl["port"].as<uint16_t>();
        if (esl["password"])           config_.esl.password           = esl["password"].as<std::string>();
        if (esl["connect_timeout_ms"]) config_.esl.connect_timeout_ms = esl["connect_timeout_ms"].as<uint32_t>();
        if (esl["sip_proxy_host"])     config_.esl.sip_proxy_host     = esl["sip_proxy_host"].as<std::string>();
        if (esl["sip_proxy_port"])     config_.esl.sip_proxy_port     = esl["sip_proxy_port"].as<uint16_t>();
    }

    if (const auto redis = gw["redis"]) {
        if (redis["enabled"])            config_.redis.enabled            = redis["enabled"].as<bool>();
        if (redis["host"])               config_.redis.host               = redis["host"].as<std::string>();
        if (redis["port"])               config_.redis.port               = redis["port"].as<uint16_t>();
        if (redis["connect_timeout_ms"]) config_.redis.connect_timeout_ms = redis["connect_timeout_ms"].as<uint32_t>();
        if (redis["command_timeout_ms"]) config_.redis.command_timeout_ms = redis["command_timeout_ms"].as<uint32_t>();
        if (redis["pool_size"])          config_.redis.pool_size          = redis["pool_size"].as<uint32_t>();
    }

    apply_env(config_);
}

TenantConfig TenantConfig::from_default(const GatewayConfig& cfg) noexcept {
    TenantConfig t;
    t.sample_rate = cfg.media.sample_rate;
    t.channels    = cfg.media.channels;
    t.frame_ms    = cfg.media.frame_ms;

    t.vad.frame_ms   = cfg.media.frame_ms;
    // VAD thresholds use EnergyVADConfig defaults (tuned for 16 kHz L16)

    t.vad_engine        = cfg.media.vad_engine;
    t.silero.model_path = cfg.media.vad_model_path;

    // timers use CallFsmTimerConfig defaults

    t.transport = cfg.conversation;

    t.backpressure.max_outbound_queue_frames = cfg.media.playback_max_frames;
    t.backpressure.max_inbound_queue_frames =
        (cfg.media.frame_ms > 0) ? cfg.media.ring_buffer_ms / cfg.media.frame_ms : 25u;

    return t;
}

TenantConfig TenantConfig::from_redis(
    RedisClient& redis, const std::string& tenant_id, const GatewayConfig& cfg,
    Logger* logger) noexcept
{
    TenantConfig t = from_default(cfg);
    t.tenant_id = tenant_id;

    try {
        const auto raw = redis.get("tenant:" + tenant_id);
        if (!raw.has_value()) return t;   // cache miss — from_default() baseline stands

        const auto j = nlohmann::json::parse(*raw);

        // Each field is optional and overlaid independently; absent fields keep defaults.
        if (j.contains("vad_engine") && j["vad_engine"].is_string())
            t.vad_engine = j["vad_engine"].get<std::string>();

        const bool silero = (t.vad_engine == "silero");
        if (j.contains("vad_onset_ms") && j["vad_onset_ms"].is_number()) {
            const auto ms = j["vad_onset_ms"].get<uint32_t>();
            if (silero) t.silero.onset_ms = ms; else t.vad.onset_ms = ms;
        }
        if (j.contains("vad_hold_ms") && j["vad_hold_ms"].is_number()) {
            const auto ms = j["vad_hold_ms"].get<uint32_t>();
            if (silero) t.silero.hold_ms = ms; else t.vad.hold_ms = ms;
        }
        if (j.contains("vad_speech_threshold") && j["vad_speech_threshold"].is_number()) {
            const auto v = j["vad_speech_threshold"].get<float>();
            if (silero) t.silero.speech_threshold = v; else t.vad.speech_threshold_db = v;
        }

        if (j.contains("no_speech_timeout_ms") && j["no_speech_timeout_ms"].is_number()) {
            const auto ms = std::chrono::milliseconds{j["no_speech_timeout_ms"].get<int64_t>()};
            if (ms < CallFsmTimerConfig::no_speech_timeout_min ||
                ms > CallFsmTimerConfig::no_speech_timeout_max) {
                if (logger)
                    logger->warn("TenantConfig: no_speech_timeout_ms={} out of bounds "
                                 "[{}, {}] for tenant={} — using default {}ms",
                                 ms.count(),
                                 CallFsmTimerConfig::no_speech_timeout_min.count(),
                                 CallFsmTimerConfig::no_speech_timeout_max.count(),
                                 tenant_id,
                                 CallFsmTimerConfig::no_speech_timeout_default.count());
                t.timers.no_speech_timeout = CallFsmTimerConfig::no_speech_timeout_default;
            } else {
                t.timers.no_speech_timeout = ms;
            }
        }
        if (j.contains("stt_timeout_ms") && j["stt_timeout_ms"].is_number())
            t.timers.stt_timeout =
                std::chrono::milliseconds{j["stt_timeout_ms"].get<int64_t>()};
        if (j.contains("llm_timeout_ms") && j["llm_timeout_ms"].is_number())
            t.timers.llm_timeout =
                std::chrono::milliseconds{j["llm_timeout_ms"].get<int64_t>()};
        if (j.contains("transfer_timeout_ms") && j["transfer_timeout_ms"].is_number()) {
            const auto ms = std::chrono::milliseconds{j["transfer_timeout_ms"].get<int64_t>()};
            if (ms < CallFsmTimerConfig::transfer_timeout_min ||
                ms > CallFsmTimerConfig::transfer_timeout_max) {
                if (logger)
                    logger->warn("TenantConfig: transfer_timeout_ms={} out of bounds "
                                 "[{}, {}] for tenant={} — using default {}ms",
                                 ms.count(),
                                 CallFsmTimerConfig::transfer_timeout_min.count(),
                                 CallFsmTimerConfig::transfer_timeout_max.count(),
                                 tenant_id,
                                 CallFsmTimerConfig::transfer_timeout_default.count());
                t.timers.transfer_timeout = CallFsmTimerConfig::transfer_timeout_default;
            } else {
                t.timers.transfer_timeout = ms;
            }
        }
    } catch (const nlohmann::json::exception&) {
        return from_default(cfg);
    }

    return t;
}

const char* to_string(RoutingStatus s) noexcept {
    switch (s) {
        case RoutingStatus::Routed:      return "routed";
        case RoutingStatus::RoutedLkg:   return "routed_lkg";
        case RoutingStatus::Unknown:     return "unknown";
        case RoutingStatus::Unavailable: return "unavailable";
    }
    return "unavailable";
}

std::optional<PhoneRoute> PhoneRoute::parse(const std::string& raw) noexcept {
    try {
        const auto j = nlohmann::json::parse(raw);
        if (!j.is_object()) return std::nullopt;
        const auto t = j.find("tenant_slug");
        const auto a = j.find("agent_slug");
        if (t == j.end() || !t->is_string() || a == j.end() || !a->is_string())
            return std::nullopt;

        PhoneRoute route;
        route.tenant_slug = t->get<std::string>();
        route.agent_slug  = a->get<std::string>();
        if (route.tenant_slug.empty() || route.agent_slug.empty()) return std::nullopt;
        if (const auto v = j.find("version"); v != j.end() && v->is_number_unsigned())
            route.version = v->get<uint32_t>();
        return route;
    } catch (const nlohmann::json::exception&) {
        return std::nullopt;
    }
}

void DidRouteCache::put(const std::string& did, const PhoneRoute& route) {
    std::lock_guard lock{mutex_};
    routes_[did] = route;
}

void DidRouteCache::erase(const std::string& did) {
    std::lock_guard lock{mutex_};
    routes_.erase(did);
}

std::optional<PhoneRoute> DidRouteCache::get(const std::string& did) const {
    std::lock_guard lock{mutex_};
    const auto it = routes_.find(did);
    if (it == routes_.end()) return std::nullopt;
    return it->second;
}

RouteResolution resolve_route(
    const std::string& did, const LookupResult& lookup, DidRouteCache& cache)
{
    if (did.empty()) return {RoutingStatus::Unknown, {}};

    switch (lookup.status) {
        case LookupStatus::Miss:
            cache.erase(did);
            return {RoutingStatus::Unknown, {}};
        case LookupStatus::Hit:
            if (auto route = PhoneRoute::parse(lookup.value)) {
                cache.put(did, *route);
                return {RoutingStatus::Routed, *route};
            }
            break;   // corrupt value: treat like an outage
        case LookupStatus::Error:
            break;
    }

    if (auto cached = cache.get(did)) return {RoutingStatus::RoutedLkg, *cached};
    return {RoutingStatus::Unavailable, {}};
}

RouteResolution resolve_route(RedisClient& redis, const std::string& did, DidRouteCache& cache) {
    if (did.empty()) return {RoutingStatus::Unknown, {}};
    return resolve_route(did, redis.get_checked("did:" + did), cache);
}

CallMetadata CallMetadata::parse(const std::optional<std::string>& raw) noexcept {
    CallMetadata md;   // defaults: did="", ani="", direction="inbound", freeswitch_host=""
    if (!raw.has_value()) return md;

    try {
        const auto j = nlohmann::json::parse(*raw);
        if (j.contains("did") && j["did"].is_string())
            md.did = j["did"].get<std::string>();
        if (j.contains("ani") && j["ani"].is_string())
            md.ani = j["ani"].get<std::string>();
        if (j.contains("direction") && j["direction"].is_string())
            md.direction = j["direction"].get<std::string>();
        if (j.contains("freeswitch_host") && j["freeswitch_host"].is_string())
            md.freeswitch_host = j["freeswitch_host"].get<std::string>();
    } catch (const nlohmann::json::exception&) {
        return CallMetadata{};
    }

    return md;
}

} // namespace voiceai
