#pragma once

#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/TransferRequest.h"

#include <mutex>
#include <string>

namespace voiceai {

// Minimal FreeSWITCH ESL (inbound mode) client over one persistent TCP connection,
// mutex-serialized because ESL handles one command at a time. Never throws.
class EslClient {
public:
    EslClient(EslConfig cfg, Logger& logger);
    ~EslClient();

    EslClient(const EslClient&)            = delete;
    EslClient& operator=(const EslClient&) = delete;

    // Thread-safe; connects lazily. No-op if disabled or uuid is empty.
    void hangup(const std::string& uuid, const std::string& reason);

    // Cold-transfers req.call_id to a number (via the SIP proxy) or SIP URI.
    // destination_problem() rejects destinations that loop back into the platform.
    // true only means FreeSWITCH accepted the command; the real outcome arrives
    // later as CHANNEL_BRIDGE/CHANNEL_HANGUP. false is final, with a code in error_out.
    bool transfer(const TransferRequest& req, std::string& error_out);

    // ── Warm transfer primitives ─────────────────────────────────────────────

    // Originates an agent leg via bgapi so ringing never blocks the shared
    // connection. Outcome arrives as BACKGROUND_JOB for out_job_uuid; false is final.
    bool originate_async(const std::string& destination, const std::string& caller_id_number,
                        std::string& out_job_uuid, std::string& error_out);

    // Only after the agent leg's CHANNEL_ANSWER.
    bool bridge(const std::string& uuid_a, const std::string& uuid_b, std::string& error_out);

    // Must complete before bridge(), or the AI keeps receiving the human-to-human audio.
    bool stop_audio_fork(const std::string& uuid, std::string& error_out);

    // FreeSWITCH-side hold/MOH for the caller's leg while the agent leg rings.
    // Interaction with an attached uuid_audio_fork is not yet live-verified.
    bool hold(const std::string& uuid, std::string& error_out);
    bool unhold(const std::string& uuid, std::string& error_out);

private:
    // "" when `destination` may be dialed, else the error code transfer() and
    // originate_async() report: invalid_destination, platform_destination, or
    // sip_proxy_host_unset (a number, with no proxy to send it to).
    std::string destination_problem(const std::string& destination) const;
    // A SIP URI as-is; a number via the SIP proxy (Kamailio), never a dialplan context.
    std::string dial_string_for(const std::string& destination) const;
    bool ensure_connected_locked();
    bool send_command_locked(const std::string& command, std::string& reply_out);
    void disconnect_locked();

    EslConfig cfg_;
    Logger&   logger_;

    std::mutex  mutex_;
    int         fd_{-1};   // -1 = not connected
    std::string read_buf_; // bytes read past the last parsed reply
};

} // namespace voiceai
