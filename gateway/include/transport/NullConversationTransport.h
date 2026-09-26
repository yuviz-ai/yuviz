#pragma once

#include "transport/IConversationTransport.h"
#include "common/NonCopyable.h"
#include "logging/Logger.h"

#include <atomic>
#include <chrono>
#include <thread>

namespace voiceai {

// Echo transport: immediately reflects inbound audio back as a TTS chunk.
// Validates the full audio path without a real AI service.
class NullConversationTransport final : public IConversationTransport,
                                        private NonCopyable {
public:
    explicit NullConversationTransport(Logger& logger);
    ~NullConversationTransport() override;

    bool start() override;
    void stop()  override;

    void open_session(const SessionContext& ctx) override;
    void send_audio(AudioFrame frame)                  override;
    void cancel_generation(const std::string& session_id) override;
    void send_playback_finished(const std::string& session_id, bool interrupted) override;
    void send_speech_ended(const std::string& session_id,
                           uint32_t duration_ms, float energy_db) override;
    void close_session(const std::string& session_id)  override;
    void send_transfer_initiated(const std::string& session_id, const std::string& transfer_type,
                                 const std::string& destination, const std::string& reason,
                                 const std::string& transfer_id) override;
    void send_transfer_completed(const std::string& session_id, const std::string& destination,
                                 const std::string& transfer_id) override;
    void send_transfer_failed(const std::string& session_id, const std::string& destination,
                              const std::string& reason, const std::string& transfer_id) override;
    void send_dtmf(const std::string& session_id, const std::string& digit) override;
    void set_callbacks(ConversationTransportCallbacks cbs) override;

private:
    Logger&                        logger_;
    ConversationTransportCallbacks callbacks_;
    std::atomic<bool>              running_{false};
    // True between the first send_audio() and the next open_session()/close_session().
    // Guards emission of stt_result + tts_started so they fire once per turn.
    bool                           turn_signalled_{false};
};

} // namespace voiceai
