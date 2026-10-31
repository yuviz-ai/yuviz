#pragma once

#include "common/AudioFrame.h"
#include "session/SessionContext.h"

#include <cstdint>
#include <functional>
#include <optional>
#include <string>

namespace voiceai {

// ServiceError code the Conversation Service sends to hang up a rejected call; not a failure.
inline constexpr const char* kCallRejectedCode = "CALL_REJECTED";

// Callbacks invoked by the transport when data arrives from the ConversationService.
struct ConversationTransportCallbacks {
    std::function<void(const std::string& session_id)> on_service_ready;

    // Recognizing → Thinking.
    std::function<void(std::string text, float confidence)> on_stt_result;

    // Thinking → Synthesizing.
    std::function<void()>                               on_tts_started;

    std::function<void(AudioFrame frame)>               on_tts_chunk;

    std::function<void(const std::string& session_id)>  on_cancel_ack;

    // Fires just before on_cancel_ack with the cancel_id the service echoed
    // (empty when it sent none).
    std::function<void(std::optional<uint64_t> cancel_id)> on_cancel_ack_id;

    // Agent ended the call; fires after the final TtsChunk. grace_period_ms 0 = default.
    std::function<void(const std::string& session_id,
                       std::string        reason,
                       uint32_t           grace_period_ms)> on_end_call;

    // Warm-only fields: empty caller_id means the caller's own ANI; empty or
    // unknown waiting_experience means "announcement_moh".
    std::function<void(const std::string& session_id,
                       std::string        transfer_type,
                       std::string        destination,
                       std::string        reason,
                       std::string        transfer_id,
                       std::string        caller_id,
                       std::string        waiting_experience)> on_transfer_requested;

    // Post-transfer cleanup finished; the gateway may tear down.
    std::function<void(const std::string& session_id)>       on_conversation_finalized;

    std::function<void(const std::string& session_id,
                       std::string        error,
                       bool               fatal)>        on_error;
};

// Outbound transport to the Python ConversationService.
class IConversationTransport {
public:
    virtual ~IConversationTransport() = default;

    virtual bool start() = 0;
    virtual void stop()  = 0;

    virtual void open_session(const SessionContext& ctx) = 0;

    // Non-blocking; may buffer.
    virtual void send_audio(AudioFrame frame) = 0;

    // Barge-in.
    virtual void cancel_generation(const std::string& session_id) = 0;

    // Barge-in whose CancelAck echoes cancel_id (see on_cancel_ack_id). False when
    // the cancel was not sent, or when no CancelAck carrying this id will come, as
    // with this default for transports that cannot carry an id.
    virtual bool send_cancel_generation(const std::string& session_id,
                                        std::optional<uint64_t> /*cancel_id*/) {
        cancel_generation(session_id);
        return false;  // no id echo, so do not hold the cancelled-reply gate
    }

    // interrupted=true when cancelled by barge-in.
    virtual void send_playback_finished(const std::string& session_id,
                                        bool interrupted) = 0;

    // As above, plus the reply audio the caller heard before an interrupt
    // (nullopt when unknown). Transports that cannot carry it keep this default.
    virtual void send_playback_finished_heard(const std::string& session_id,
                                              bool interrupted,
                                              std::optional<uint64_t> /*played_bytes*/) {
        send_playback_finished(session_id, interrupted);
    }

    // Sent once per utterance, after all its AudioChunks.
    virtual void send_speech_ended(const std::string& session_id,
                                   uint32_t duration_ms,
                                   float    energy_db) = 0;

    virtual void close_session(const std::string& session_id) = 0;

    // Transfer lifecycle notifications. Initiated is sent as soon as the transfer
    // is issued; completed/failed once the real outcome is known.
    virtual void send_transfer_initiated(const std::string& session_id,
                                         const std::string& transfer_type,
                                         const std::string& destination,
                                         const std::string& reason,
                                         const std::string& transfer_id) = 0;
    virtual void send_transfer_completed(const std::string& session_id,
                                         const std::string& destination,
                                         const std::string& transfer_id) = 0;
    virtual void send_transfer_failed(const std::string& session_id,
                                      const std::string& destination,
                                      const std::string& reason,
                                      const std::string& transfer_id) = 0;

    virtual void send_dtmf(const std::string& session_id,
                           const std::string& digit) = 0;

    // Must be called before start(); callbacks are stored without a mutex.
    virtual void set_callbacks(ConversationTransportCallbacks cbs) = 0;
};

} // namespace voiceai
