#pragma once

#include <chrono>
#include <functional>
#include <string>

namespace voiceai {

// Shared vocabulary for the cold and warm transfer strategies.

enum class LegRole { Customer, Agent };

enum class LegState { Originating, Ringing, Answered, Bridged, Failed, Hungup };

struct CallLeg {
    std::string uuid;
    LegRole     role;
    LegState    state;
};

// Where the caller's audio flows. BridgePending = fork stop issued, not yet confirmed.
enum class MediaOwnership { Gateway, BridgePending, FreeSwitch };

enum class CoordinatorState { Idle, Active, Completed };

struct TransferCoordinatorContext {
    std::string call_id;       // customer leg's FreeSWITCH uuid (== ctx_.obs.call_id)
    std::string destination;
    std::string reason;
    std::string transfer_id;   // observability-only correlation id
    // Warm-only. caller_id is already resolved non-empty by CallSession;
    // waiting_experience is the raw config value.
    std::string caller_id;
    std::string waiting_experience;
};

// Coordinators never touch transport/FSM directly; CallSession owns those effects.
struct TransferCoordinatorCallbacks {
    // Fired exactly once per attempt. Empty destination means "use the one from start()".
    std::function<void(bool success, std::string destination, std::string detail)>
        on_transfer_completed;

    // Fired at most once, when the customer's SIP leg is handed outside the Gateway,
    // so the resulting WebSocket close isn't treated as a hangup. May be unset.
    std::function<void()> on_media_handoff;
};

// One transfer strategy's orchestration.
class ITransferCoordinator {
public:
    virtual ~ITransferCoordinator() = default;

    // May complete synchronously or asynchronously. Don't call again until the
    // previous attempt is terminal.
    virtual void start(TransferCoordinatorContext ctx,
                       TransferCoordinatorCallbacks callbacks) = 0;

    // Fails an in-flight attempt with "cancelled"; no-op when idle.
    virtual void cancel() = 0;

    // Idempotent; guarantees no callback fires after it returns. Does not fire
    // on_transfer_completed.
    virtual void shutdown() = 0;

    [[nodiscard]] virtual CoordinatorState state() const noexcept = 0;
};

} // namespace voiceai
