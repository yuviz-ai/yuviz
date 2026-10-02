#include "telephony/WarmTransferCoordinator.h"

namespace voiceai {

WarmTransferCoordinator::WarmTransferCoordinator(
    EslClient& esl_client, TransferCorrelator& leg_correlator,
    TransferCorrelator& job_correlator, ContextLogger& log)
    : esl_client_(esl_client)
    , leg_correlator_(leg_correlator)
    , job_correlator_(job_correlator)
    , log_(log)
{}

void WarmTransferCoordinator::start(TransferCoordinatorContext ctx,
                                    TransferCoordinatorCallbacks callbacks) {
    state_              = CoordinatorState::Active;
    media_owner_        = MediaOwnership::Gateway;
    customer_uuid_      = ctx.call_id;
    destination_        = ctx.destination;
    transfer_id_        = ctx.transfer_id;
    caller_id_          = ctx.caller_id;
    waiting_experience_ = ctx.waiting_experience;
    agent_uuid_.clear();
    job_uuid_.clear();
    callbacks_      = std::move(callbacks);

    // MOH while the agent leg rings; a failed hold just degrades to silence.
    if (waiting_experience_ != "announcement_silence") {
        std::string hold_error;
        if (!esl_client_.hold(customer_uuid_, hold_error)) {
            log_.warn("Warm transfer: hold failed uuid={} error={} transfer_id={} — "
                      "continuing without MOH", customer_uuid_, hold_error, transfer_id_);
        }
    }

    std::string error;
    if (!esl_client_.originate_async(destination_, caller_id_, job_uuid_, error)) {
        log_.warn("Warm transfer: originate not accepted destination={} error={} "
                 "transfer_id={}", destination_, error, transfer_id_);
        finish(false, destination_, error);
        return;
    }

    // Job-UUID only exists after originate's reply, so this is the earliest watch point.
    job_correlator_.watch(job_uuid_, [this](bool success, std::string result) {
        on_job_resolved(success, std::move(result));
    });
}

void WarmTransferCoordinator::on_job_resolved(bool success, std::string result) {
    // originate only succeeds once the destination answers (&park() runs
    // post-answer), so success here is the answer confirmation.
    if (!success) {
        log_.warn("Warm transfer: originate failed destination={} detail={} transfer_id={}",
                 destination_, result, transfer_id_);
        // A held channel won't play the apology TTS, so always unhold.
        std::string unhold_error;
        if (!esl_client_.unhold(customer_uuid_, unhold_error)) {
            log_.warn("Warm transfer: unhold failed uuid={} error={} transfer_id={} — "
                      "caller may remain on hold", customer_uuid_, unhold_error, transfer_id_);
        }
        finish(false, destination_, result.empty() ? "no_answer" : result);
        return;
    }

    agent_uuid_ = std::move(result);
    log_.info("Warm transfer: agent leg answered agent_uuid={} destination={} transfer_id={}",
             agent_uuid_, destination_, transfer_id_);

    std::string unhold_error;
    if (!esl_client_.unhold(customer_uuid_, unhold_error)) {
        log_.warn("Warm transfer: unhold failed uuid={} error={} transfer_id={} — "
                  "proceeding anyway", customer_uuid_, unhold_error, transfer_id_);
    }

    // Stop the fork before bridging, or human-to-human audio leaks into the AI.
    // on_media_handoff must fire first: stopping the fork closes the WebSocket.
    media_owner_ = MediaOwnership::BridgePending;
    if (callbacks_.on_media_handoff) callbacks_.on_media_handoff();
    std::string fork_error;
    if (!esl_client_.stop_audio_fork(customer_uuid_, fork_error)) {
        log_.warn("Warm transfer: stop_audio_fork failed uuid={} error={} transfer_id={} — "
                  "bridging anyway (AI may briefly receive post-bridge audio)",
                  customer_uuid_, fork_error, transfer_id_);
    }

    std::string bridge_error;
    if (!esl_client_.bridge(customer_uuid_, agent_uuid_, bridge_error)) {
        log_.warn("Warm transfer: bridge failed customer_uuid={} agent_uuid={} error={} "
                 "transfer_id={}", customer_uuid_, agent_uuid_, bridge_error, transfer_id_);
        esl_client_.hangup(agent_uuid_, "bridge_failed");
        finish(false, destination_, "bridge_failed:" + bridge_error);
        return;
    }

    media_owner_ = MediaOwnership::FreeSwitch;
    finish(true, destination_, "bridged");
}

void WarmTransferCoordinator::cancel() {
    if (state_ != CoordinatorState::Active) return;

    log_.info("Warm transfer: cancelled agent_uuid={} job_uuid={} transfer_id={}",
             agent_uuid_, job_uuid_, transfer_id_);

    // A late BACKGROUND_JOB resolves into nothing: finish() removes the watch.
    if (!agent_uuid_.empty()) {
        esl_client_.hangup(agent_uuid_, "transfer_cancelled");
    }
    std::string unhold_error;
    esl_client_.unhold(customer_uuid_, unhold_error);

    finish(false, destination_, "cancelled");
}

void WarmTransferCoordinator::finish(bool success, std::string destination, std::string detail) {
    state_ = CoordinatorState::Completed;
    if (!job_uuid_.empty()) job_correlator_.cancel(job_uuid_);
    if (!agent_uuid_.empty()) leg_correlator_.cancel(agent_uuid_);
    if (callbacks_.on_transfer_completed)
        callbacks_.on_transfer_completed(success, std::move(destination), std::move(detail));
}

void WarmTransferCoordinator::shutdown() {
    if (state_ == CoordinatorState::Idle) return;
    // No callbacks: called from ~CallSession(), so calling back is unsafe.
    if (!job_uuid_.empty()) job_correlator_.cancel(job_uuid_);
    if (!agent_uuid_.empty()) leg_correlator_.cancel(agent_uuid_);
    state_ = CoordinatorState::Completed;
}

CoordinatorState WarmTransferCoordinator::state() const noexcept {
    return state_;
}

} // namespace voiceai
