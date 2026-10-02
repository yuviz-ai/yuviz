#pragma once

#include "metrics/IMetrics.h"

#include <string>

namespace voiceai {

// Metrics wrapper that prefixes every name with "<tenant_id>.".
class SessionMetrics {
public:
    SessionMetrics(IMetrics& metrics, std::string tenant_id)
        : metrics_(metrics)
        , prefix_ (std::move(tenant_id) + ".")
    {}

    void increment(const std::string& name, double value = 1.0) {
        metrics_.increment(prefix_ + name, value);
    }

    void gauge(const std::string& name, double value) {
        metrics_.gauge(prefix_ + name, value);
    }

    void observe(const std::string& name, double value) {
        metrics_.observe(prefix_ + name, value);
    }

private:
    IMetrics&   metrics_;
    std::string prefix_;
};

} // namespace voiceai
