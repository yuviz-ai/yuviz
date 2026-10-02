#pragma once

#include "transport/IConversationTransport.h"
#include "logging/Logger.h"

#include <functional>
#include <memory>
#include <shared_mutex>
#include <string>
#include <unordered_map>

namespace voiceai {

// Thread-safe registry of IConversationTransport creators keyed by type name
// ("null" from Application, "grpc" from main.cpp).
class ConversationTransportFactory {
public:
    using Creator = std::function<std::unique_ptr<IConversationTransport>(Logger&)>;

    ConversationTransportFactory() = default;

    void register_provider(const std::string& type, Creator creator) {
        std::unique_lock lock{mutex_};
        registry_[type] = std::move(creator);
    }

    [[nodiscard]] std::unique_ptr<IConversationTransport>
    create(const std::string& type, Logger& logger) const {
        std::shared_lock lock{mutex_};
        auto it = registry_.find(type);
        if (it == registry_.end())
            throw std::runtime_error("Unknown conversation transport type: " + type);
        return it->second(logger);
    }

    [[nodiscard]] bool has(const std::string& type) const {
        std::shared_lock lock{mutex_};
        return registry_.count(type) > 0;
    }

private:
    mutable std::shared_mutex                  mutex_;
    std::unordered_map<std::string, Creator>   registry_;
};

} // namespace voiceai
