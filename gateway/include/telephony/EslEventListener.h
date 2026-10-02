#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/TransferCorrelator.h"

#include <atomic>
#include <functional>
#include <string>
#include <thread>

namespace voiceai {

// Listens for CHANNEL_HANGUP/BRIDGE/ANSWER and BACKGROUND_JOB on a dedicated ESL
// connection (events can't share EslClient's request/reply connection).
//   - CHANNEL_HANGUP without a transfer watch → on_hangup (real-time hangup detection).
//   - Channel uuid watches: BRIDGE/ANSWER = success, HANGUP = failure.
//   - Job-UUID watches resolve from the BACKGROUND_JOB body ("-ERR" = failure).
// If ESL is down, nothing fires; CallFSM timeouts are the backstop. Never throws.
class EslEventListener : private NonCopyable, private NonMovable {
public:
    using HangupHandler = std::function<void(const std::string& uuid)>;
    using DtmfHandler = std::function<void(const std::string& uuid, const std::string& digit)>;

    EslEventListener(EslConfig cfg, Logger& logger, HangupHandler on_hangup,
                      DtmfHandler on_dtmf,
                      TransferCorrelator& transfer_correlator,
                      TransferCorrelator& job_correlator);
    ~EslEventListener();

    // Returns true without starting if disabled.
    bool start();

    // Idempotent; joins the thread.
    void stop();

private:
    void run_loop();
    bool connect_and_subscribe();

    EslConfig           cfg_;
    Logger&             logger_;
    HangupHandler       on_hangup_;
    DtmfHandler         on_dtmf_;
    TransferCorrelator& transfer_correlator_;
    TransferCorrelator& job_correlator_;

    std::atomic<bool> running_{false};
    std::thread       worker_;
    int               fd_{-1};
    std::string       read_carry_;  // bytes read past the last parsed frame
};

} // namespace voiceai
