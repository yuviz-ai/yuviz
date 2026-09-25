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

// Subscribes to FreeSWITCH's CHANNEL_HANGUP, CHANNEL_BRIDGE, CHANNEL_ANSWER,
// and BACKGROUND_JOB events over a dedicated ESL connection (deliberately
// separate from EslClient's command connection — ESL's request/reply
// protocol on one connection cannot be mixed with an unsolicited event
// stream on the same connection) and:
//
//   - invokes on_hangup with the hanging-up channel's UUID the moment
//     FreeSWITCH reports a CHANNEL_HANGUP for it (unless that uuid has a
//     pending transfer watch — see below, where the event means something
//     more specific and on_hangup is deliberately skipped for it);
//   - resolves any pending transfer_correlator watch for a channel uuid:
//     CHANNEL_BRIDGE/CHANNEL_ANSWER = success (a cold transfer bridged, or
//     a warm transfer's agent leg answered), CHANNEL_HANGUP = failure (the
//     channel dropped before ever bridging/answering — busy, rejected, no
//     answer, invalid destination, etc.);
//   - resolves any pending job_correlator watch for a bgapi Job-UUID when
//     BACKGROUND_JOB arrives — warm transfer's originate_async() outcome
//     (see docs/warm_transfer_architecture.md §3/§6). Success/failure is
//     read from the job body itself ("-ERR ..." vs. the new channel's own
//     uuid on success), not from whether the command was merely accepted
//     (that's originate_async()'s own synchronous return value — see its
//     doc comment).
//
// CHANNEL_HANGUP detection closes the real-time detection gap
// EslClient::hangup() alone can't: without it, the Gateway only learns a
// caller has hung up via no_speech_timeout (a full silence-timeout window —
// potentially a minute or more) or a WebSocket close mod_audio_fork may
// send late or not send at all.
//
// CHANNEL_BRIDGE/CHANNEL_HANGUP-for-transfers closes a different gap: a
// uuid_transfer command's "+OK" reply (see EslClient::transfer()) means
// FreeSWITCH *accepted* the command, not that the destination actually
// answered — uuid_transfer is asynchronous. Trusting "+OK" as "the transfer
// succeeded" is telephony-incorrect; this class is what lets CallSession
// wait for the real outcome instead (see TransferCorrelator).
//
// Degrades safely, matching EslClient's own posture: if cfg.enabled is
// false, or the connection repeatedly fails, on_hangup simply never fires
// and no transfer watch is ever resolved by an event — a pending transfer
// still resolves via CallFSM's own TransferTimeout timer regardless (the
// backstop for "no event ever arrives," same principle as
// no_speech_timeout backstopping caller-hangup detection). Never throws,
// never blocks the caller of start()/stop() beyond a normal thread join.
class EslEventListener : private NonCopyable, private NonMovable {
public:
    using HangupHandler = std::function<void(const std::string& uuid)>;
    using DtmfHandler = std::function<void(const std::string& uuid, const std::string& digit)>;

    EslEventListener(EslConfig cfg, Logger& logger, HangupHandler on_hangup,
                      DtmfHandler on_dtmf,
                      TransferCorrelator& transfer_correlator,
                      TransferCorrelator& job_correlator);
    ~EslEventListener();

    // Spawns the background listener thread. No-op (returns true) if
    // cfg.enabled is false — same "absence is fine" contract as EslClient.
    bool start();

    // Graceful, idempotent stop — joins the background thread.
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
    std::string       read_carry_;  // bytes read past the last parsed frame, carried to the next read
};

} // namespace voiceai
