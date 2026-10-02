#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "transport/IConversationTransport.h"

namespace grpc { class Channel; }

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace voiceai {

// Bidi-streaming gRPC transport, one per CallSession over a shared channel.
// writer_loop() is the sole Write() caller after open_session(); close_session()
// uses TryCancel() so its thread joins are near-instant.
class GrpcConversationTransport final
    : public IConversationTransport
    , private NonCopyable
    , private NonMovable
{
public:
    GrpcConversationTransport(std::shared_ptr<grpc::Channel> channel, Logger& logger);
    ~GrpcConversationTransport() override;

    // IConversationTransport
    bool start() override { return true; }  // channel is always ready
    void stop()  override;
    void open_session(const SessionContext& ctx) override;
    void send_audio(AudioFrame frame) override;
    void cancel_generation(const std::string& session_id) override;
    void send_playback_finished(const std::string& session_id, bool interrupted) override;
    void send_speech_ended(const std::string& session_id,
                           uint32_t duration_ms, float energy_db) override;
    void close_session(const std::string& session_id) override;
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
    void reader_loop() noexcept;
    void writer_loop() noexcept;

    std::shared_ptr<grpc::Channel> channel_;
    Logger&                        logger_;

    ConversationTransportCallbacks callbacks_;
    std::string                    session_id_;

    // Opaque to keep grpc headers out of this header.
    struct StreamState;
    std::unique_ptr<StreamState>   stream_;

    struct SendQueue;
    std::unique_ptr<SendQueue>     send_queue_;

    std::thread            reader_thread_;
    std::thread            writer_thread_;
    std::atomic<bool>      stream_open_{false};
    std::atomic<bool>      writer_stopping_{false};
    // Lets close_session() wait briefly so final messages send before TryCancel().
    std::atomic<bool>      reader_done_{false};
};

} // namespace voiceai
