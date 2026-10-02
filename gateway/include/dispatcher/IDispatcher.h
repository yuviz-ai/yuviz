#pragma once

#include "core/IComponent.h"
#include "events/SessionEvent.h"

#include <cstdint>
#include <functional>

namespace voiceai {

// Event bus; subscribers run on a dedicated worker thread, not the caller's.
// Raw PCM never passes through here.
class IDispatcher : public IComponent {
public:
    using Subscriber   = std::function<void(const SessionEvent&)>;
    using SubscriberId = uint32_t;

    virtual ~IDispatcher() = default;

    // Post an event to the queue.  Thread-safe; never blocks the caller.
    virtual void dispatch(SessionEvent event) = 0;

    virtual SubscriberId subscribe(Subscriber handler)     = 0;
    virtual void         unsubscribe(SubscriberId id)      = 0;
};

} // namespace voiceai
