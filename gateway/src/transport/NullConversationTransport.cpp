#include "transport/NullConversationTransport.h"

namespace voiceai {

NullConversationTransport::NullConversationTransport(Logger& logger)
    : logger_(logger)
{}

NullConversationTransport::~NullConversationTransport() {
    stop();
}

bool NullConversationTransport::start() {
    running_.store(true);
    logger_.info("NullConversationTransport started (echo mode)");
    return true;
}

void NullConversationTransport::stop() {
    running_.store(false);
}

void NullConversationTransport::open_session(const SessionContext& ctx) {
    logger_.debug("NullConversationTransport::open_session session={}", ctx.obs.session_id);
    turn_signalled_ = false;
    if (callbacks_.on_service_ready)
        callbacks_.on_service_ready(ctx.obs.session_id);
}

void NullConversationTransport::send_audio(AudioFrame frame) {
    if (!running_.load()) return;

    // On the first audio frame of each turn, fire stt_result then tts_started
    // so the Gateway FSM advances Recognizing→Thinking→Synthesizing before the
    // echo TTS chunk drives it to Speaking.
    if (!turn_signalled_) {
        turn_signalled_ = true;
        if (callbacks_.on_stt_result)
            callbacks_.on_stt_result("echo", 1.0f);
        if (callbacks_.on_tts_started)
            callbacks_.on_tts_started();
    }

    // Echo the inbound frame back as a TTS chunk (direction flipped).
    frame.direction = AudioDirection::Outbound;
    if (callbacks_.on_tts_chunk)
        callbacks_.on_tts_chunk(std::move(frame));
}

void NullConversationTransport::cancel_generation(const std::string& session_id) {
    logger_.debug("NullConversationTransport::cancel_generation session={}", session_id);
    turn_signalled_ = false;
    if (callbacks_.on_cancel_ack)
        callbacks_.on_cancel_ack(session_id);
}

void NullConversationTransport::send_playback_finished(const std::string& session_id,
                                                       bool /*interrupted*/) {
    logger_.debug("NullConversationTransport::send_playback_finished session={}", session_id);
    // Reset so the next turn's first audio frame re-fires stt_result + tts_started.
    turn_signalled_ = false;
}

void NullConversationTransport::send_speech_ended(const std::string& session_id,
                                                  uint32_t /*duration_ms*/,
                                                  float    /*energy_db*/) {
    // Echo mode drives the pipeline via send_audio() — no explicit speech_ended
    // handling needed.  The real pipeline uses GrpcConversationTransport.
    logger_.debug("NullConversationTransport::send_speech_ended session={}", session_id);
}

void NullConversationTransport::close_session(const std::string& session_id) {
    logger_.debug("NullConversationTransport::close_session session={}", session_id);
    turn_signalled_ = false;
}

void NullConversationTransport::send_transfer_initiated(const std::string& session_id,
                                                        const std::string& transfer_type,
                                                        const std::string& destination,
                                                        const std::string& /*reason*/,
                                                        const std::string& /*transfer_id*/) {
    logger_.debug("NullConversationTransport::send_transfer_initiated session={} type={} destination={}",
                  session_id, transfer_type, destination);
}

void NullConversationTransport::send_transfer_completed(const std::string& session_id,
                                                        const std::string& destination,
                                                        const std::string& /*transfer_id*/) {
    logger_.debug("NullConversationTransport::send_transfer_completed session={} destination={}",
                  session_id, destination);
}

void NullConversationTransport::send_transfer_failed(const std::string& session_id,
                                                      const std::string& destination,
                                                      const std::string& /*reason*/,
                                                      const std::string& /*transfer_id*/) {
    logger_.debug("NullConversationTransport::send_transfer_failed session={} destination={}",
                  session_id, destination);
}

void NullConversationTransport::send_dtmf(const std::string& session_id,
                                          const std::string& digit) {
    logger_.debug("NullConversationTransport::send_dtmf session={} digit={}", session_id, digit);
}

void NullConversationTransport::set_callbacks(ConversationTransportCallbacks cbs) {
    callbacks_ = std::move(cbs);
}

} // namespace voiceai
