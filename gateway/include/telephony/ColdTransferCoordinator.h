#pragma once

#include "logging/ContextLogger.h"
#include "telephony/EslClient.h"
#include "telephony/ITransferCoordinator.h"
#include "telephony/TransferCorrelator.h"

#include <atomic>

namespace voiceai {

// uuid_transfer cold transfer. The AI leg ends once the command is accepted, so
// there's nothing to recover if the destination never answers.
class ColdTransferCoordinator final : public ITransferCoordinator {
public:
    ColdTransferCoordinator(EslClient& esl_client, TransferCorrelator& correlator,
                            ContextLogger& log);

    void start(TransferCoordinatorContext ctx, TransferCoordinatorCallbacks callbacks) override;
    void cancel() override;
    void shutdown() override;
    [[nodiscard]] CoordinatorState state() const noexcept override;

private:
    EslClient&           esl_client_;
    TransferCorrelator&  correlator_;
    ContextLogger&        log_;

    // Atomic: read cross-thread by ~CallSession().
    std::atomic<CoordinatorState> state_{CoordinatorState::Idle};
    std::string                   active_call_id_;
    TransferCoordinatorCallbacks  callbacks_;
};

} // namespace voiceai
