#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"

#include <functional>
#include <mutex>
#include <string>
#include <unordered_map>

namespace voiceai {

// Thread-safe uuid -> one-shot resolution handler registry, routing async ESL
// events to the waiting CallSession.
class TransferCorrelator : private NonCopyable, private NonMovable {
public:
    using ResolutionHandler = std::function<void(bool success, std::string detail)>;

    TransferCorrelator() = default;

    // Overwrites any existing watch for the same uuid.
    void watch(const std::string& uuid, ResolutionHandler on_resolved);

    // Removes a watch without firing it.
    void cancel(const std::string& uuid);

    // Returns false if nothing was pending. The handler runs outside the lock,
    // so it may re-enter this class.
    bool resolve(const std::string& uuid, bool success, std::string detail);

private:
    std::mutex                                         mutex_;
    std::unordered_map<std::string, ResolutionHandler> pending_;
};

} // namespace voiceai
