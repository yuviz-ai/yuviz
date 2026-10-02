#pragma once

#include "core/IComponent.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "session/CallSession.h"
#include "session/CallSessionFactory.h"
#include "session/SessionContext.h"
#include "websocket/IWebSocketConnection.h"

#include <memory>
#include <shared_mutex>
#include <string>
#include <unordered_map>

namespace voiceai {

// Owns the lifetime of live CallSession objects.
class SessionManager : public IComponent, private NonCopyable, private NonMovable {
public:
    explicit SessionManager(std::unique_ptr<CallSessionFactory> factory, Logger& logger);
    ~SessionManager() override = default;

    bool initialize() override;
    bool start()      override;
    void stop()       override;
    void shutdown()   override;

    // Keyed by the WebSocketServer connection id (what set_on_disconnect hands
    // back), not ctx.obs.session_id (the FreeSWITCH channel UUID).
    void create(const std::string& conn_id, SessionContext ctx, std::shared_ptr<IWebSocketConnection> connection);

    // No-op if not found. ~CallSession() runs outside the sessions lock.
    void remove(const std::string& conn_id);

    // Terminates the session whose channel UUID matches call_id. Linear scan is
    // fine: low call volume and once per hangup. No-op if none matches.
    void terminate_by_call_id(const std::string& call_id, const std::string& reason);

    void push_dtmf_to_call(const std::string& call_id, const std::string& digit);

    [[nodiscard]] size_t active_count() const;

private:
    std::unique_ptr<CallSessionFactory>                               factory_;
    Logger&                                                           logger_;
    mutable std::shared_mutex                                         mutex_;
    std::unordered_map<std::string, std::unique_ptr<CallSession>>     sessions_;
};

} // namespace voiceai
