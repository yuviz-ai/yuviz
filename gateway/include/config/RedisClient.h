#pragma once

#include "config/Config.h"
#include "logging/Logger.h"

#include <condition_variable>
#include <deque>
#include <mutex>
#include <optional>
#include <string>

// Forward-declare hiredis's C struct so this header stays hiredis-free for
// everything that just calls get() — only RedisClient.cpp includes <hiredis/hiredis.h>.
struct redisContext;

namespace voiceai {

class RedisClient {
public:
    RedisClient(RedisConfig cfg, Logger& logger);
    ~RedisClient();

    RedisClient(const RedisClient&)            = delete;
    RedisClient& operator=(const RedisClient&) = delete;

    // Returns the string value at `key`, or std::nullopt if the key doesn't
    // exist, Redis is unreachable, or cfg.enabled is false.
    [[nodiscard]] std::optional<std::string> get(const std::string& key);

    // Like get(), but tells a missing key apart from an unreachable or disabled Redis.
    [[nodiscard]] LookupResult get_checked(const std::string& key);

    // Like get_checked(), but a missing key is a Miss only if guard_key exists; else Error.
    [[nodiscard]] LookupResult get_guarded(const std::string& key, const char* guard_key);

private:
    struct Connection {
        redisContext* ctx{nullptr};
    };

    LookupResult lookup(const std::string& key, const char* guard_key);
    bool ensure_connected(Connection& conn);
    void disconnect(Connection& conn);

    RedisConfig cfg_;
    Logger&     logger_;

    std::mutex              pool_mutex_;
    std::condition_variable pool_cv_;
    std::deque<Connection>  idle_;
};

} // namespace voiceai
