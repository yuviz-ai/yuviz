#pragma once

#include "logging/ContextLogger.h"
#include "telephony/EslClient.h"
#include "telephony/ITransferCoordinator.h"
#include "telephony/TransferCorrelator.h"

#include <atomic>

namespace voiceai {

// Attended transfer: originates a separate agent leg and bridges only once it
// answers, so on failure the caller's leg is untouched and the AI can recover.
//   start → hold → originate_async → BACKGROUND_JOB ok → unhold →
//   stop_audio_fork (confirmed) → bridge → completed(true)
//   failure → kill agent leg → unhold → completed(false)
class WarmTransferCoordinator final : public ITransferCoordinator {
public:
    WarmTransferCoordinator(EslClient& esl_client, TransferCorrelator& leg_correlator,
                            TransferCorrelator& job_correlator, ContextLogger& log);

    void start(TransferCoordinatorContext ctx, TransferCoordinatorCallbacks callbacks) override;
    void cancel() override;
    void shutdown() override;
    [[nodiscard]] CoordinatorState state() const noexcept override;

private:
    void on_job_resolved(bool success, std::string result);
    void finish(bool success, std::string destination, std::string detail);

    EslClient&           esl_client_;
    TransferCorrelator&  leg_correlator_;   // agent-leg uuid keyspace (shared with cold)
    TransferCorrelator&  job_correlator_;   // bgapi Job-UUID keyspace
    ContextLogger&        log_;

    // Atomic: written on the ESL thread, read by ~CallSession() on the cleanup pool.
    std::atomic<CoordinatorState> state_{CoordinatorState::Idle};
    MediaOwnership   media_owner_{MediaOwnership::Gateway};
    std::string      customer_uuid_;   // ctx.call_id — never redirected, only held/unheld
    std::string      job_uuid_;
    std::string      agent_uuid_;      // known only after BACKGROUND_JOB succeeds
    std::string      destination_;
    std::string       transfer_id_;
    std::string       caller_id_;
    std::string       waiting_experience_;
    TransferCoordinatorCallbacks callbacks_;
};

} // namespace voiceai
