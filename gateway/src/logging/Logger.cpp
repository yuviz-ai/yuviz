#include "logging/Logger.h"
#include <spdlog/sinks/null_sink.h>
#include <spdlog/sinks/rotating_file_sink.h>
#include <spdlog/sinks/stdout_color_sinks.h>
#include <atomic>
#include <stdexcept>
#include <vector>

namespace voiceai {

namespace {

spdlog::level::level_enum to_spdlog_level(Logger::Level level) {
    switch (level) {
        case Logger::Level::Trace:    return spdlog::level::trace;
        case Logger::Level::Debug:    return spdlog::level::debug;
        case Logger::Level::Info:     return spdlog::level::info;
        case Logger::Level::Warn:     return spdlog::level::warn;
        case Logger::Level::Error:    return spdlog::level::err;
        case Logger::Level::Critical: return spdlog::level::critical;
        case Logger::Level::Off:      return spdlog::level::off;
    }
    return spdlog::level::info;
}

} // namespace

Logger::Logger(const std::string& name) {
    logger_ = spdlog::stdout_color_mt(name);
    logger_->set_pattern("[%Y-%m-%d %H:%M:%S.%e] [%n] [%^%l%$] %v");
}

Logger::Logger(const std::string& name, const std::string& file_path, bool console) {
    std::vector<spdlog::sink_ptr> sinks;

    if (console) {
        sinks.push_back(std::make_shared<spdlog::sinks::stdout_color_sink_mt>());
    }
    if (!file_path.empty()) {
        constexpr size_t max_size  = 64 * 1024 * 1024; // 64 MB
        constexpr size_t max_files = 5;
        sinks.push_back(std::make_shared<spdlog::sinks::rotating_file_sink_mt>(
            file_path, max_size, max_files));
    }

    logger_ = std::make_shared<spdlog::logger>(name, sinks.begin(), sinks.end());
    logger_->set_pattern("[%Y-%m-%d %H:%M:%S.%e] [%n] [%^%l%$] %v");
    spdlog::register_logger(logger_);
}

void Logger::set_level(Level level) {
    logger_->set_level(to_spdlog_level(level));
}

Logger Logger::make_null() {
    // Counter keeps names unique for spdlog's registry.
    static std::atomic<uint64_t> seq{0};
    auto sink = std::make_shared<spdlog::sinks::null_sink_mt>();
    Logger l;
    l.logger_ = std::make_shared<spdlog::logger>(
        "null_" + std::to_string(seq.fetch_add(1, std::memory_order_relaxed)),
        std::move(sink));
    l.logger_->set_level(spdlog::level::off);
    return l;
}

} // namespace voiceai
