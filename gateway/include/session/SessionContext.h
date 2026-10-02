#pragma once

#include "config/Config.h"
#include "observability/ObservabilityContext.h"

#include <memory>
#include <string>

namespace voiceai {

// Immutable per-session identity, routing, and config snapshot.
struct SessionContext {
    ObservabilityContext                    obs;     // session_id, tenant_id, trace_id, call_id
    std::shared_ptr<const TenantConfig>    tenant;
    std::string                            caller_did;
    std::string                            called_did;
    std::string                            direction{"inbound"};
    std::string                            script_id;   // conversation script / persona for this tenant
};

} // namespace voiceai
