#pragma once

#include "common/IClock.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "dispatcher/IDispatcher.h"
#include "logging/Logger.h"
#include "media/AudioWorkerPool.h"
#include "metrics/IMetrics.h"
#include "session/CallSession.h"
#include "session/SessionContext.h"
#include "telephony/EslClient.h"
#include "telephony/TransferCorrelator.h"
#include "timer/ITimerService.h"
#include "transport/ConversationTransportFactory.h"
#include "websocket/IWebSocketConnection.h"

#include <memory>

namespace voiceai {

// Encapsulates CallSession's many-parameter construction.
// TODO: replace transport_factory_.create() with a per-tenant gRPC channel lookup.
class CallSessionFactory : private NonCopyable, private NonMovable {
public:
    CallSessionFactory(ConversationTransportFactory& transport_factory,
                       AudioWorkerPool&              worker_pool,
                       ITimerService&                timer_svc,
                       IDispatcher&                  dispatcher,
                       IMetrics&                     metrics,
                       IClock&                       clock,
                       Logger&                       logger,
                       EslClient&                    esl_client,
                       TransferCorrelator&            transfer_correlator,
                       TransferCorrelator&            job_correlator);

    [[nodiscard]] std::unique_ptr<CallSession> create(
        SessionContext                        ctx,
        std::shared_ptr<IWebSocketConnection> connection);

private:
    ConversationTransportFactory& transport_factory_;
    AudioWorkerPool&              worker_pool_;
    ITimerService&                timer_svc_;
    IDispatcher&                  dispatcher_;
    IMetrics&                     metrics_;
    IClock&                       clock_;
    Logger&                       logger_;
    EslClient&                    esl_client_;
    TransferCorrelator&           transfer_correlator_;
    TransferCorrelator&           job_correlator_;
};

} // namespace voiceai
