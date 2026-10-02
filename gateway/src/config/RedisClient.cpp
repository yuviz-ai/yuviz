#include "config/RedisClient.h"

#include <hiredis/hiredis.h>

#include <memory>

namespace voiceai {

namespace {
constexpr uint32_t kMaxPoolSize = 32;
}

RedisClient::RedisClient(RedisConfig cfg, Logger& logger)
    : cfg_(std::move(cfg))
    , logger_(logger)
{
    uint32_t n = cfg_.pool_size > 0 ? cfg_.pool_size : 1;
    if (n > kMaxPoolSize) {
        logger_.warn("RedisClient: pool_size={} exceeds max {}, clamping", n, kMaxPoolSize);
        n = kMaxPoolSize;
    }
    for (uint32_t i = 0; i < n; ++i) idle_.push_back(Connection{});
}

RedisClient::~RedisClient() {
    std::lock_guard lock{pool_mutex_};
    for (auto& conn : idle_) disconnect(conn);
}

void RedisClient::disconnect(Connection& conn) {
    if (conn.ctx != nullptr) {
        ::redisFree(conn.ctx);
        conn.ctx = nullptr;
    }
}

bool RedisClient::ensure_connected(Connection& conn) {
    if (conn.ctx != nullptr) return true;

    const timeval connect_tv{
        static_cast<time_t>(cfg_.connect_timeout_ms / 1000),
        static_cast<suseconds_t>((cfg_.connect_timeout_ms % 1000) * 1000),
    };
    redisContext* ctx = ::redisConnectWithTimeout(cfg_.host.c_str(), cfg_.port, connect_tv);
    if (ctx == nullptr) {
        logger_.warn("RedisClient: redisConnectWithTimeout returned null host={} port={}",
                     cfg_.host, cfg_.port);
        return false;
    }
    if (ctx->err != 0) {
        logger_.warn("RedisClient: connect failed host={} port={} err={}",
                     cfg_.host, cfg_.port, ctx->errstr);
        ::redisFree(ctx);
        return false;
    }

    const timeval command_tv{
        static_cast<time_t>(cfg_.command_timeout_ms / 1000),
        static_cast<suseconds_t>((cfg_.command_timeout_ms % 1000) * 1000),
    };
    ::redisSetTimeout(ctx, command_tv);

    conn.ctx = ctx;
    logger_.info("RedisClient: connected host={} port={}", cfg_.host, cfg_.port);
    return true;
}

std::optional<std::string> RedisClient::get(const std::string& key) {
    if (!cfg_.enabled) return std::nullopt;

    Connection conn;
    {
        std::unique_lock lock{pool_mutex_};
        pool_cv_.wait(lock, [this] { return !idle_.empty(); });
        conn = idle_.front();
        idle_.pop_front();
    }

    // Returns conn to the pool even if something below throws.
    struct ConnReturner {
        RedisClient* self;
        Connection*  conn;
        ~ConnReturner() {
            std::lock_guard lock{self->pool_mutex_};
            self->idle_.push_back(*conn);
            self->pool_cv_.notify_one();
        }
    } returner{this, &conn};

    std::optional<std::string> result;
    if (ensure_connected(conn)) {
        redisReply* reply = static_cast<redisReply*>(
            ::redisCommand(conn.ctx, "GET %s", key.c_str()));

        if (reply == nullptr) {
            // hiredis convention: a null reply means the connection died mid-command.
            logger_.warn("RedisClient: GET failed key={} (connection lost, will reconnect)", key);
            disconnect(conn);
        } else {
            std::unique_ptr<redisReply, void(*)(void*)> reply_guard{reply, ::freeReplyObject};
            if (reply->type == REDIS_REPLY_STRING) {
                result = std::string(reply->str, static_cast<size_t>(reply->len));
            } else if (reply->type == REDIS_REPLY_NIL) {
                result = std::nullopt;   // cache miss — not an error
            } else if (reply->type == REDIS_REPLY_ERROR) {
                logger_.warn("RedisClient: GET error key={} err={}", key, reply->str);
            }
        }
    }

    return result;
}

} // namespace voiceai
