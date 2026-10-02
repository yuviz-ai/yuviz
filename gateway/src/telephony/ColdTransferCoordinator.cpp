#include "telephony/ColdTransferCoordinator.h"

namespace voiceai {

ColdTransferCoordinator::ColdTransferCoordinator(
    EslClient& esl_client, TransferCorrelator& correlator, ContextLogger& log)
    : esl_client_(esl_client), correlator_(correlator), log_(log)
{}

void ColdTransferCoordinator::start(TransferCoordinatorContext ctx,
                                    TransferCoordinatorCallbacks callbacks) {
    state_           = CoordinatorState::Active;
    active_call_id_  = ctx.call_id;
    callbacks_       = std::move(callbacks);

    // Watch before issuing the command: the outcome event arrives on another
    // thread and can beat transfer()'s own reply.
    correlator_.watch(ctx.call_id,
        [this, destination = ctx.destination](bool success, std::string detail) {
            state_ = CoordinatorState::Completed;
            if (success) {
                log_.info("Transfer confirmed destination={} detail={}", destination, detail);
            } else {
                log_.warn("Transfer failed destination={} detail={}", destination, detail);
            }
            if (callbacks_.on_transfer_completed)
                callbacks_.on_transfer_completed(success, destination, std::move(detail));
        });

    TransferRequest req{ctx.call_id, "cold", ctx.destination, ctx.reason, ctx.transfer_id};
    std::string error;
    const bool accepted = esl_client_.transfer(req, error);
    if (accepted) {
        // uuid_transfer may tear down mod_audio_fork's WebSocket before
        // CHANNEL_BRIDGE confirms the outcome.
        if (callbacks_.on_media_handoff) callbacks_.on_media_handoff();
    } else {
        // No async event will arrive for a rejected command; resolve now
        // rather than waiting for CallFSM's TransferTimeout.
        correlator_.cancel(ctx.call_id);
        state_ = CoordinatorState::Completed;
        log_.warn("Transfer command not accepted destination={} error={} transfer_id={}",
                 ctx.destination, error, ctx.transfer_id);
        if (callbacks_.on_transfer_completed)
            callbacks_.on_transfer_completed(false, ctx.destination, std::move(error));
    }
}

void ColdTransferCoordinator::cancel() {
    // No-op: uuid_transfer is fire-and-forget once issued; nothing safe to abort.
}

void ColdTransferCoordinator::shutdown() {
    if (state_ == CoordinatorState::Idle) return;
    correlator_.cancel(active_call_id_);
    state_ = CoordinatorState::Completed;
}

CoordinatorState ColdTransferCoordinator::state() const noexcept {
    return state_;
}

} // namespace voiceai
