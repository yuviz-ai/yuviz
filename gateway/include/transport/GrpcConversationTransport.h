#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "transport/IConversationTransport.h"

// Forward-declare gRPC types so callers don't need to include grpc headers.
namespace grpc { class Channel; }

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace voiceai {

// gRPC bidirectional-streaming implementation of IConversationTransport.
//
// One instance per CallSession.  All instances share the same underlying
// gRPC Channel (HTTP/2 multiplexed) which is injected at construction.
// The stream is opened lazily on open_session() and torn down on close_session().
//
// Threading model:
//   open_session():     writes SessionOpenRequest synchronously (writer thread not yet running).
//   writer_loop():      sole caller of stream->rw->Write() after open_session() returns.
//                       Woken by send_audio() / cancel_generation() via send_cv_.
//   reader_loop():      blocks on stream->rw->Read(); exits when the server closes the
//                       stream or when close_session() calls ctx.TryCancel().
//   close_session():    signals writer (sets writer_stopping_, notifies), joins writer,
//                       calls WritesDone() + ctx.TryCancel() to unblock the reader,
//                       then joins reader.  Safe to call from the lws service thread
//                       because TryCancel() makes both joins near-instant.
class GrpcConversationTransport final
    : public IConversationTransport
    , private NonCopyable
    , private NonMovable
{
public:
    // channel — shared across transports for HTTP/2 multiplexing.
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

    // Stream state — opaque struct defined in .cpp to keep grpc headers out of this header.
    struct StreamState;
    std::unique_ptr<StreamState>   stream_;

    // Send queue — opaque struct defined in .cpp (holds SPSCQueue<GatewayMessage,64>
    // + the CV used to wake the writer thread without polling).
    struct SendQueue;
    std::unique_ptr<SendQueue>     send_queue_;

    std::thread            reader_thread_;
    std::thread            writer_thread_;
    std::atomic<bool>      stream_open_{false};
    std::atomic<bool>      writer_stopping_{false};
    // Set by reader_loop() on exit — close_session() polls it briefly after
    // WritesDone() so final queued messages (TransferFailed etc.) actually
    // transmit before the TryCancel() fallback can discard them.
    std::atomic<bool>      reader_done_{false};
};

} // namespace voiceai
