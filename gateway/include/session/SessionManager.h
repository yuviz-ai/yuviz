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

// Owns the set of live CallSession objects.
// CallSessionFactory (injected) handles construction; SessionManager handles
// lifetime: create() registers, remove() destroys, shutdown() drains all.
class SessionManager : public IComponent, private NonCopyable, private NonMovable {
public:
    explicit SessionManager(std::unique_ptr<CallSessionFactory> factory, Logger& logger);
    ~SessionManager() override = default;

    bool initialize() override;
    bool start()      override;
    void stop()       override;
    void shutdown()   override;

    // Create, register, and start a new session, keyed by conn_id — the
    // WebSocketServer connection handle (WebSocketServer's own monotonic
    // counter, see next_session_id() in WebSocketServer.cpp), NOT
    // ctx.obs.session_id. Those are deliberately different identifiers:
    // ctx.obs.session_id is the FreeSWITCH channel UUID, the DB/
    // observability-facing identity; conn_id is a purely internal,
    // ephemeral bookkeeping key that must match what
    // WebSocketServer::set_on_disconnect's callback hands back on close,
    // so remove() can actually find the session it was called for.
    void create(const std::string& conn_id, SessionContext ctx, std::shared_ptr<IWebSocketConnection> connection);

    // Remove and destroy the session with the given conn_id.  No-op if not
    // found. ~CallSession() runs outside the sessions lock to avoid
    // priority inversion.
    void remove(const std::string& conn_id);

    // Finds the live session whose session_id() (the FreeSWITCH channel
    // UUID — see the create() comment above) matches call_id, and posts a
    // clean shutdown request to it — the same terminate() a caller-hangup
    // WebSocket close would trigger, just reached via EslEventListener's
    // CHANNEL_HANGUP notification instead of (or ahead of) that close
    // event or the no_speech_timeout fallback. A linear scan over
    // sessions_ is deliberate, not an oversight: concurrent call volume on
    // one Gateway process is small (tens, not thousands), and this fires
    // once per hangup, never on a hot path — not worth a second index to
    // maintain in create()/remove(). No-op (logged) if no session matches,
    // e.g. the session already tore itself down for an unrelated reason.
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
