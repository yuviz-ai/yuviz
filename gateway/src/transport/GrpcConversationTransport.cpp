#include "transport/GrpcConversationTransport.h"
#include "common/SPSCQueue.h"
#include "common/ThreadUtils.h"

#include "voiceai/v1/conversation.grpc.pb.h"
#include "voiceai/v1/conversation.pb.h"

#include <grpcpp/grpcpp.h>

#include <chrono>
#include <stdexcept>
#include <thread>

namespace voiceai {

// ── Opaque structs (defined here so grpc/proto headers stay out of .h) ────────

struct GrpcConversationTransport::StreamState {
    grpc::ClientContext ctx;
    std::unique_ptr<grpc::ClientReaderWriter<
        ::voiceai::v1::GatewayMessage,
        ::voiceai::v1::ServiceMessage>> rw;
};

// Single-producer (control/AudioWorker thread) single-consumer (writer_loop)
// queue.  Capacity 256 covers >5 s of 20ms audio frames per session before
// dropping, which prevents speech_ended loss during audio bursts.
struct GrpcConversationTransport::SendQueue {
    SPSCQueue<::voiceai::v1::GatewayMessage, 256> q;
    std::mutex              mutex;
    std::condition_variable cv;
};

// ── Construction / destruction ────────────────────────────────────────────────

GrpcConversationTransport::GrpcConversationTransport(
    std::shared_ptr<grpc::Channel> channel, Logger& logger)
    : channel_(std::move(channel))
    , logger_(logger)
{}

GrpcConversationTransport::~GrpcConversationTransport() {
    if (stream_open_.load(std::memory_order_acquire))
        close_session(session_id_);
}

void GrpcConversationTransport::stop() {
    // Idempotent: close_session() returns immediately if stream is already closed.
    close_session(session_id_);
}

void GrpcConversationTransport::set_callbacks(ConversationTransportCallbacks cbs) {
    callbacks_ = std::move(cbs);
}

// ── Session lifecycle ─────────────────────────────────────────────────────────

void GrpcConversationTransport::open_session(const SessionContext& ctx) {
    session_id_ = ctx.obs.session_id;

    send_queue_      = std::make_unique<SendQueue>();
    writer_stopping_.store(false, std::memory_order_relaxed);
    reader_done_.store(false, std::memory_order_relaxed);

    auto stub  = ::voiceai::v1::ConversationService::NewStub(channel_);
    auto state = std::make_unique<StreamState>();
    state->rw  = stub->Converse(&state->ctx);
    stream_    = std::move(state);

    // Write SessionOpenRequest synchronously before starting the writer thread
    // (no contention: writer_thread_ not yet running).
    {
        ::voiceai::v1::GatewayMessage msg;
        auto* req = msg.mutable_session_open();
        req->set_protocol_version("1.0");
        req->set_session_id(ctx.obs.session_id);
        req->set_tenant_id(ctx.obs.tenant_id);
        req->set_trace_id(ctx.obs.trace_id);
        req->set_call_id(ctx.obs.call_id);
        req->set_caller_did(ctx.caller_did);
        req->set_called_did(ctx.called_did);
        req->set_direction(ctx.direction);
        req->set_script_id(ctx.script_id);
        req->set_codec(::voiceai::v1::AUDIO_CODEC_PCM_S16LE);
        req->set_sample_rate(16000);
        req->set_channels(1);

        if (!stream_->rw->Write(msg)) {
            logger_.error("GrpcTransport: failed to write SessionOpenRequest session={}",
                          ctx.obs.session_id);
            if (callbacks_.on_error)
                callbacks_.on_error(ctx.obs.session_id, "session_open_write_failed", true);
            return;
        }
    }

    stream_open_.store(true, std::memory_order_release);

    reader_thread_ = std::thread([this] { reader_loop(); });
    writer_thread_ = std::thread([this] { writer_loop(); });
}

void GrpcConversationTransport::close_session(const std::string& /*session_id*/) {
    if (!stream_open_.exchange(false, std::memory_order_acq_rel)) return;

    // 1. Signal and join the writer thread so no more Write() calls are in-flight.
    writer_stopping_.store(true, std::memory_order_release);
    if (send_queue_) send_queue_->cv.notify_all();
    if (writer_thread_.joinable()) writer_thread_.join();

    // 2. Tell the server no more client messages are coming.
    stream_->rw->WritesDone();

    // 3. Give the server a short, bounded window to observe the half-close
    //    and end the stream on its own (the Conversation Service returns
    //    from Converse() as soon as its request iterator ends, so this
    //    normally resolves in single-digit ms on loopback). Cancelling
    //    immediately raced the final queued message — Write() only means
    //    "accepted by gRPC", and TryCancel() can discard an accepted-but-
    //    untransmitted frame. Observed live 2026-07-18: a TransferFailed
    //    sent right before close_session() intermittently never reached
    //    the service, silently losing the failure outcome (no apology
    //    path, no TRANSFER_TIMEOUT persistence).
    for (int i = 0; i < 50 && !reader_done_.load(std::memory_order_acquire); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds{5});

    // 4. Fallback: cancel the RPC context so a still-blocked Read() in
    //    reader_loop() returns (with CANCELLED status). No-op when the
    //    stream already finished naturally above. Without this bound,
    //    join() could stall the lws thread if the server never closes.
    stream_->ctx.TryCancel();
    if (reader_thread_.joinable()) reader_thread_.join();

    // 5. Collect final RPC status (may be CANCELLED — that is expected here).
    auto status = stream_->rw->Finish();
    if (!status.ok() && status.error_code() != grpc::StatusCode::CANCELLED) {
        logger_.warn("GrpcTransport: stream finished with error: {} session={}",
                     status.error_message(), session_id_);
    }
    stream_.reset();
    // send_queue_ is intentionally NOT reset here.  The control thread may
    // still have queued send_audio() lambdas that will run after this returns;
    // they check stream_open_=false and return early, but they may still
    // dereference send_queue_.  The unique_ptr is reset safely by the destructor
    // AFTER the control thread is joined in ~CallSession().
}

// ── Send path ─────────────────────────────────────────────────────────────────

void GrpcConversationTransport::send_audio(AudioFrame frame) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    auto* chunk = msg.mutable_audio_chunk();
    chunk->set_session_id(frame.session_id.data());
    chunk->set_trace_id(frame.trace_id.data());
    chunk->set_sequence_num(frame.sequence_num);
    chunk->set_timestamp_us(frame.timestamp_us);
    chunk->set_payload(frame.payload.data(), frame.payload.size());

    if (!send_queue_->q.push(std::move(msg)))
        logger_.warn("GrpcTransport: send_queue_ full, dropping audio session={}", session_id_);
    else
        send_queue_->cv.notify_one();
}

void GrpcConversationTransport::cancel_generation(const std::string& session_id) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    msg.mutable_cancel_generation()->set_session_id(session_id);

    if (!send_queue_->q.push(std::move(msg)))
        logger_.warn("GrpcTransport: send_queue_ full, dropping cancel_generation session={}", session_id);
    else
        send_queue_->cv.notify_one();
}

void GrpcConversationTransport::send_playback_finished(const std::string& session_id,
                                                        bool interrupted) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    auto* pf = msg.mutable_playback_finished();
    pf->set_session_id(session_id);
    pf->set_interrupted(interrupted);

    if (!send_queue_->q.push(std::move(msg)))
        logger_.warn("GrpcTransport: send_queue_ full, dropping playback_finished session={}", session_id);
    else
        send_queue_->cv.notify_one();
}

void GrpcConversationTransport::send_speech_ended(const std::string& session_id,
                                                   uint32_t duration_ms,
                                                   float    energy_db) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    auto* se = msg.mutable_speech_ended();
    se->set_session_id(session_id);
    se->set_duration_ms(duration_ms);
    se->set_energy_db(energy_db);

    if (!send_queue_->q.push(std::move(msg)))
        logger_.error("GrpcTransport: send_queue_ full, dropping speech_ended session={}", session_id);
    else
        send_queue_->cv.notify_one();
}

void GrpcConversationTransport::send_transfer_initiated(const std::string& session_id,
                                                        const std::string& transfer_type,
                                                        const std::string& destination,
                                                        const std::string& reason,
                                                        const std::string& transfer_id) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    auto* ti = msg.mutable_transfer_initiated();
    ti->set_session_id(session_id);
    ti->set_transfer_type(transfer_type);
    ti->set_destination(destination);
    ti->set_reason(reason);
    ti->set_transfer_id(transfer_id);

    if (!send_queue_->q.push(std::move(msg)))
        logger_.warn("GrpcTransport: send_queue_ full, dropping transfer_initiated session={}", session_id);
    else
        send_queue_->cv.notify_one();
}

void GrpcConversationTransport::send_transfer_completed(const std::string& session_id,
                                                        const std::string& destination,
                                                        const std::string& transfer_id) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    auto* tc = msg.mutable_transfer_completed();
    tc->set_session_id(session_id);
    tc->set_destination(destination);
    tc->set_transfer_id(transfer_id);

    if (!send_queue_->q.push(std::move(msg)))
        logger_.warn("GrpcTransport: send_queue_ full, dropping transfer_completed session={}", session_id);
    else
        send_queue_->cv.notify_one();
}

void GrpcConversationTransport::send_transfer_failed(const std::string& session_id,
                                                     const std::string& destination,
                                                     const std::string& reason,
                                                     const std::string& transfer_id) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    auto* tf = msg.mutable_transfer_failed();
    tf->set_session_id(session_id);
    tf->set_destination(destination);
    tf->set_reason(reason);
    tf->set_transfer_id(transfer_id);

    if (!send_queue_->q.push(std::move(msg)))
        logger_.warn("GrpcTransport: send_queue_ full, dropping transfer_failed session={}", session_id);
    else
        send_queue_->cv.notify_one();
}

void GrpcConversationTransport::send_dtmf(const std::string& session_id,
                                          const std::string& digit) {
    if (!stream_open_.load(std::memory_order_acquire)) return;

    ::voiceai::v1::GatewayMessage msg;
    auto* dtmf = msg.mutable_dtmf();
    dtmf->set_session_id(session_id);
    dtmf->set_digit(digit);

    if (!send_queue_->q.push(std::move(msg)))
        logger_.warn("GrpcTransport: send_queue_ full, dropping dtmf session={} digit={}", session_id, digit);
    else
        send_queue_->cv.notify_one();
}

// ── Writer loop ───────────────────────────────────────────────────────────────

void GrpcConversationTransport::writer_loop() noexcept {
    set_thread_name("GrpcWriter");

    bool write_failed = false;
    while (!writer_stopping_.load(std::memory_order_acquire)) {
        auto item = send_queue_->q.pop();
        if (!item) {
            // Queue is empty — wait for a notification or the 1ms timeout.
            // The timeout ensures we check writer_stopping_ even if notify is lost.
            std::unique_lock lock{send_queue_->mutex};
            send_queue_->cv.wait_for(lock, std::chrono::milliseconds{1},
                [this] { return writer_stopping_.load(std::memory_order_acquire); });
            continue;
        }
        if (!stream_->rw->Write(*item)) {
            logger_.warn("GrpcTransport: Write failed session={}", session_id_);
            if (callbacks_.on_error)
                callbacks_.on_error(session_id_, "grpc_write_failed", true);
            write_failed = true;
            break;
        }
    }

    // Drain messages enqueued before the stop signal — but only on clean shutdown.
    // Calling Write() after a failed Write() is undefined behaviour per gRPC contract.
    if (!write_failed) {
        while (auto item = send_queue_->q.pop()) {
            stream_->rw->Write(*item);
        }
    }
}

// ── Reader loop ───────────────────────────────────────────────────────────────

void GrpcConversationTransport::reader_loop() noexcept {
    set_thread_name("GrpcReader");

    ::voiceai::v1::ServiceMessage msg;
    while (stream_->rw->Read(&msg)) {
        switch (msg.payload_case()) {
        case ::voiceai::v1::ServiceMessage::kServiceReady:
            logger_.info("GrpcTransport: ServiceReady session={}", session_id_);
            if (callbacks_.on_service_ready)
                callbacks_.on_service_ready(session_id_);
            break;

        case ::voiceai::v1::ServiceMessage::kTtsChunk: {
            const auto& chunk = msg.tts_chunk();
            AudioFrame frame;
            frame.set_session_id(chunk.session_id());
            frame.set_trace_id(chunk.trace_id());
            frame.sequence_num = chunk.sequence_num();
            frame.sample_rate  = chunk.sample_rate();
            frame.direction    = AudioDirection::Outbound;
            frame.codec        = AudioCodec::PCM_S16LE;
            frame.is_final     = chunk.is_final();
            const auto& pl     = chunk.payload();
            frame.payload.assign(
                reinterpret_cast<const uint8_t*>(pl.data()),
                reinterpret_cast<const uint8_t*>(pl.data()) + pl.size());
            if (callbacks_.on_tts_chunk)
                callbacks_.on_tts_chunk(std::move(frame));
            break;
        }

        case ::voiceai::v1::ServiceMessage::kSttResult: {
            const auto& r = msg.stt_result();
            if (callbacks_.on_stt_result)
                callbacks_.on_stt_result(r.text(), r.confidence());
            break;
        }

        case ::voiceai::v1::ServiceMessage::kTtsStarted:
            if (callbacks_.on_tts_started)
                callbacks_.on_tts_started();
            break;

        case ::voiceai::v1::ServiceMessage::kCancelAck:
            if (callbacks_.on_cancel_ack)
                callbacks_.on_cancel_ack(session_id_);
            break;

        case ::voiceai::v1::ServiceMessage::kEndCall:
            if (callbacks_.on_end_call)
                callbacks_.on_end_call(session_id_, msg.end_call().reason(),
                                       msg.end_call().grace_period_ms());
            break;

        case ::voiceai::v1::ServiceMessage::kTransferRequest: {
            const auto& tr = msg.transfer_request();
            logger_.info("GrpcTransport: TransferRequest type={} destination={} transfer_id={} "
                        "caller_id={} waiting_experience={} session={}",
                         tr.transfer_type(), tr.destination(), tr.transfer_id(),
                         tr.caller_id(), tr.waiting_experience(), session_id_);
            if (callbacks_.on_transfer_requested)
                callbacks_.on_transfer_requested(session_id_, tr.transfer_type(),
                                                 tr.destination(), tr.reason(),
                                                 tr.transfer_id(), tr.caller_id(),
                                                 tr.waiting_experience());
            break;
        }

        case ::voiceai::v1::ServiceMessage::kConversationFinalized: {
            const auto& cf = msg.conversation_finalized();
            // reason/summary_generated/transcript_written are informational
            // only — the gateway's own teardown doesn't branch on them, it
            // just needed to know cleanup is done (see CallFSM's Finalizing
            // state). Logged so operators can see e.g. a fallback summary
            // was used without digging through the Conversation Service's
            // own logs.
            logger_.info("GrpcTransport: ConversationFinalized session={} reason={} "
                         "summary_generated={} transcript_written={}",
                         session_id_,
                         ::voiceai::v1::FinalizationReason_Name(cf.reason()),
                         cf.summary_generated(), cf.transcript_written());
            if (callbacks_.on_conversation_finalized)
                callbacks_.on_conversation_finalized(session_id_);
            break;
        }

        case ::voiceai::v1::ServiceMessage::kError: {
            const auto& err = msg.error();
            logger_.error("GrpcTransport: service error code={} msg={} fatal={} session={}",
                          err.code(), err.message(), err.fatal(), session_id_);
            if (callbacks_.on_error)
                callbacks_.on_error(session_id_, err.message(), err.fatal());
            break;
        }

        default:
            logger_.warn("GrpcTransport: unknown payload_case={} session={}",
                         static_cast<int>(msg.payload_case()), session_id_);
            break;
        }
        msg.Clear();
    }
    reader_done_.store(true, std::memory_order_release);
    logger_.debug("GrpcTransport: reader_loop exited session={}", session_id_);
}

} // namespace voiceai
