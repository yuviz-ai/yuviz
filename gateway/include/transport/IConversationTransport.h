#pragma once

#include "common/AudioFrame.h"
#include "session/SessionContext.h"

#include <functional>
#include <string>

namespace voiceai {

// Callbacks invoked by the transport when data arrives from the ConversationService.
struct ConversationTransportCallbacks {
    // ConversationService is ready to accept audio for this session.
    std::function<void(const std::string& session_id)> on_service_ready;

    // STT transcript ready — drives Gateway FSM Recognizing → Thinking.
    std::function<void(std::string text, float confidence)> on_stt_result;

    // TTS synthesis is about to begin — drives Gateway FSM Thinking → Synthesizing.
    std::function<void()>                               on_tts_started;

    // TTS audio chunk from ConversationService → push to PlaybackQueue.
    std::function<void(AudioFrame frame)>               on_tts_chunk;

    // ConversationService acknowledges barge-in cancellation.
    std::function<void(const std::string& session_id)>  on_cancel_ack;

    // The agent has decided the conversation is over.  Fires after the final
    // TtsChunk of the turn; the gateway waits for that audio to finish
    // playing before tearing down the session.  grace_period_ms: 0 = use
    // the gateway's own configured default (CallFsmTimerConfig::goodbye_
    // timeout); non-zero overrides it for this call (see EndCall in the
    // gRPC protocol).
    std::function<void(const std::string& session_id,
                       std::string        reason,
                       uint32_t           grace_period_ms)> on_end_call;

    // The ConversationService has requested handing the call to a human
    // (an LLM-emitted [[TRANSFER]] directive, or an escalation-threshold
    // breach — see services/conversation/pipeline.py). Moves CallFSM into
    // Transferring and executes the transfer over ESL (uuid_transfer) —
    // see CallSession's wire_fsm_handlers()/on_transfer_requested.
    // caller_id: what caller ID a warm transfer's agent leg should show —
    // already fully resolved by the Conversation Service (see
    // transfer_engine.py's _resolve_caller_id()); the gateway never sees
    // the policy behind it. Empty means "use the caller's own ANI"
    // (ctx_.caller_did) — CallSession's own fallback, not something this
    // callback or its caller need to reason about. Unused for cold
    // transfer (no equivalent — uuid_transfer never originates a new leg).
    //
    // waiting_experience: unlike caller_id, NOT resolved by the
    // Conversation Service — the raw agent.transfer_waiting_experience
    // value ("announcement_moh"/"announcement_silence") rides through
    // unchanged, because whether to issue uuid_hold is a telephony-
    // execution decision WarmTransferCoordinator makes for itself. Empty
    // or unrecognized means "announcement_moh". Unused for cold transfer.
    std::function<void(const std::string& session_id,
                       std::string        transfer_type,
                       std::string        destination,
                       std::string        reason,
                       std::string        transfer_id,
                       std::string        caller_id,
                       std::string        waiting_experience)> on_transfer_requested;

    // Phase 5D: the Conversation Service has finished all post-call
    // cleanup after a successful transfer (see session_finalizer.py) and
    // is telling the gateway it may now proceed with its own teardown.
    std::function<void(const std::string& session_id)>       on_conversation_finalized;

    // Transport-level error (may be fatal).
    std::function<void(const std::string& session_id,
                       std::string        error,
                       bool               fatal)>        on_error;
};

// Interface for the outbound transport to the Python ConversationService.
// One logical connection per gateway process; sessions are multiplexed on it.
//
// Implementations: NullConversationTransport (echo, no AI service) and
// GrpcConversationTransport (real bidirectional gRPC stream).
class IConversationTransport {
public:
    virtual ~IConversationTransport() = default;

    virtual bool start() = 0;
    virtual void stop()  = 0;

    // Notify ConversationService that a new call session is starting.
    // Full SessionContext is passed so the transport can forward caller_did,
    // called_did, and script_id to the service alongside the trace IDs.
    virtual void open_session(const SessionContext& ctx) = 0;

    // Send an inbound audio frame to the ConversationService (STT input).
    // Non-blocking; transport may buffer internally.
    virtual void send_audio(AudioFrame frame) = 0;

    // Signal barge-in: ConversationService should cancel generation and re-listen.
    virtual void cancel_generation(const std::string& session_id) = 0;

    // Notify ConversationService that the last TTS chunk has been played.
    // interrupted=true when cancelled by barge-in; false for natural completion.
    virtual void send_playback_finished(const std::string& session_id,
                                        bool interrupted) = 0;

    // Notify ConversationService that a speech utterance has ended.
    // The service should run STT on its accumulated audio buffer and begin the
    // STT → LLM → TTS pipeline.  Called once per utterance, after all AudioChunk
    // messages for that utterance have been delivered.
    virtual void send_speech_ended(const std::string& session_id,
                                   uint32_t duration_ms,
                                   float    energy_db) = 0;

    // Notify ConversationService that the session is ending (clean teardown).
    virtual void close_session(const std::string& session_id) = 0;

    // Transfer lifecycle notifications (Phase 5B of AI-to-human transfer) —
    // purely gateway-to-service synchronization about what the telephony
    // layer is doing with this call; the service only reacts (drives its
    // own ConversationFSM's TRANSFERRING state, publishes observability
    // events — see session.py). No LLM/prompt change, no fallback speech,
    // no workflow change results from these on the service side.
    //
    // Sent by CallSession's on_transfer_requested/on_transfer_completed FSM
    // handlers (see CallSession.cpp) — send_transfer_initiated() as soon as
    // uuid_transfer has been issued (this is the service's one chance to
    // react before the gateway closes the AI session, which may happen
    // immediately after a confirmed outcome — see Phase 5A);
    // send_transfer_completed()/send_transfer_failed() once the real
    // outcome is confirmed (a CHANNEL_BRIDGE/CHANNEL_HANGUP event, or
    // CallFSM's own TransferTimeout with no event ever arriving).
    // transfer_id: observability-only correlation id, echoed verbatim from
    // the TransferRequest that started this attempt (see conversation.proto).
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

    // ORDERING CONSTRAINT: set_callbacks() MUST be called before start().
    // Implementations store callbacks without a mutex, relying on this pre-start
    // ordering to establish a happens-before relationship: the lock acquired
    // inside start() (to create reader threads) synchronises with those threads,
    // making the callbacks visible to them.
    // Calling set_callbacks() concurrently with any other method, or after
    // start(), is a data race on the std::function members.
    virtual void set_callbacks(ConversationTransportCallbacks cbs) = 0;
};

} // namespace voiceai
