#include "session/SessionManager.h"

namespace voiceai {

SessionManager::SessionManager(std::unique_ptr<CallSessionFactory> factory, Logger& logger)
    : factory_(std::move(factory))
    , logger_ (logger)
{}

bool SessionManager::initialize() { return true; }

bool SessionManager::start() {
    logger_.info("SessionManager started");
    return true;
}

void SessionManager::stop() {
    logger_.info("SessionManager stopped — no longer accepting new sessions");
}

void SessionManager::shutdown() {
    // Swap out the session map so destruction runs outside the lock.
    std::unordered_map<std::string, std::unique_ptr<CallSession>> sessions;
    {
        std::unique_lock lock{mutex_};
        sessions.swap(sessions_);
    }
    logger_.info("SessionManager shutdown — {} sessions terminated", sessions.size());
    // ~CallSession() runs for each entry as the local map goes out of scope.
}

void SessionManager::create(const std::string& conn_id, SessionContext ctx, std::shared_ptr<IWebSocketConnection> connection) {
    const std::string session_id = ctx.obs.session_id;  // for the log line only
    auto session = factory_->create(std::move(ctx), std::move(connection));
    {
        std::unique_lock lock{mutex_};
        sessions_.emplace(conn_id, std::move(session));
    }
    logger_.info("SessionManager: created session conn_id={} session_id={} total={}",
                 conn_id, session_id, active_count());
}

void SessionManager::remove(const std::string& conn_id) {
    std::unique_ptr<CallSession> session;
    {
        std::unique_lock lock{mutex_};
        auto it = sessions_.find(conn_id);
        if (it == sessions_.end()) return;
        session = std::move(it->second);
        sessions_.erase(it);
    }
    // ~CallSession() runs here, outside the lock.
    logger_.info("SessionManager: removed session conn_id={} remaining={}", conn_id, active_count());
}

void SessionManager::terminate_by_call_id(const std::string& call_id, const std::string& reason) {
    std::shared_lock lock{mutex_};
    for (auto& [conn_id, session] : sessions_) {
        if (session->session_id() == call_id) {
            session->terminate(reason);
            return;
        }
    }
    logger_.info("SessionManager: terminate_by_call_id found no live session call_id={} reason={}",
                 call_id, reason);
}

void SessionManager::push_dtmf_to_call(const std::string& call_id, const std::string& digit) {
    std::shared_lock lock{mutex_};
    for (auto& [conn_id, session] : sessions_) {
        if (session->session_id() == call_id) {
            session->push_dtmf(digit);
            return;
        }
    }
}

size_t SessionManager::active_count() const {
    std::shared_lock lock{mutex_};
    return sessions_.size();
}

} // namespace voiceai
