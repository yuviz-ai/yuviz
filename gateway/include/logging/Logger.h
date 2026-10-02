#pragma once

#include <memory>
#include <string>
#include <spdlog/spdlog.h>

namespace voiceai {

class Logger {
public:
    enum class Level { Trace, Debug, Info, Warn, Error, Critical,
                       Off };  // Off — discard all output

    explicit Logger(const std::string& name);
    Logger(const std::string& name, const std::string& file_path, bool console);
    ~Logger() = default;

    Logger(const Logger&)            = delete;
    Logger& operator=(const Logger&) = delete;
    Logger(Logger&&)                 = default;
    Logger& operator=(Logger&&)      = default;

    // Silent logger (null sink, level Off) for tests and benchmarks.
    [[nodiscard]] static Logger make_null();

    void set_level(Level level);

    template<typename... Args>
    void trace(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_->trace(fmt, std::forward<Args>(args)...);
    }

    template<typename... Args>
    void debug(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_->debug(fmt, std::forward<Args>(args)...);
    }

    template<typename... Args>
    void info(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_->info(fmt, std::forward<Args>(args)...);
    }

    template<typename... Args>
    void warn(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_->warn(fmt, std::forward<Args>(args)...);
    }

    template<typename... Args>
    void error(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_->error(fmt, std::forward<Args>(args)...);
    }

    template<typename... Args>
    void critical(spdlog::format_string_t<Args...> fmt, Args&&... args) {
        logger_->critical(fmt, std::forward<Args>(args)...);
    }

    [[nodiscard]] spdlog::logger& raw() noexcept { return *logger_; }

private:
    Logger() = default;  // used by make_null()
    std::shared_ptr<spdlog::logger> logger_;
};

} // namespace voiceai
