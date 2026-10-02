#pragma once

#include "logging/Logger.h"
#include "observability/ObservabilityContext.h"

#include <spdlog/fmt/fmt.h>

namespace voiceai {

// Logger wrapper that prefixes every line with the observability IDs.
// set_provider_request_id() must not run concurrently with any log method.
class ContextLogger {
public:
    ContextLogger(const ObservabilityContext& obs, Logger& logger)
        : obs_(obs), logger_(logger)
    {}

    template<typename... Args>
    void trace(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_.trace("[s={} t={} tr={} c={} r={}] {}", obs_.session_id, obs_.tenant_id,
            obs_.trace_id, obs_.call_id, obs_.provider_request_id,
            ::fmt::format(fmt, std::forward<Args>(args)...));
    }

    template<typename... Args>
    void debug(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_.debug("[s={} t={} tr={} c={} r={}] {}", obs_.session_id, obs_.tenant_id,
            obs_.trace_id, obs_.call_id, obs_.provider_request_id,
            ::fmt::format(fmt, std::forward<Args>(args)...));
    }

    template<typename... Args>
    void info(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_.info("[s={} t={} tr={} c={} r={}] {}", obs_.session_id, obs_.tenant_id,
            obs_.trace_id, obs_.call_id, obs_.provider_request_id,
            ::fmt::format(fmt, std::forward<Args>(args)...));
    }

    template<typename... Args>
    void warn(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_.warn("[s={} t={} tr={} c={} r={}] {}", obs_.session_id, obs_.tenant_id,
            obs_.trace_id, obs_.call_id, obs_.provider_request_id,
            ::fmt::format(fmt, std::forward<Args>(args)...));
    }

    template<typename... Args>
    void error(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_.error("[s={} t={} tr={} c={} r={}] {}", obs_.session_id, obs_.tenant_id,
            obs_.trace_id, obs_.call_id, obs_.provider_request_id,
            ::fmt::format(fmt, std::forward<Args>(args)...));
    }

    template<typename... Args>
    void critical(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_.critical("[s={} t={} tr={} c={} r={}] {}", obs_.session_id, obs_.tenant_id,
            obs_.trace_id, obs_.call_id, obs_.provider_request_id,
            ::fmt::format(fmt, std::forward<Args>(args)...));
    }

    // Allow updating provider_request_id between STT/LLM/TTS turns.
    void set_provider_request_id(std::string id) { obs_.provider_request_id = std::move(id); }

    [[nodiscard]] Logger& underlying() noexcept { return logger_; }

private:
    ObservabilityContext obs_;
    Logger&              logger_;
};

} // namespace voiceai
