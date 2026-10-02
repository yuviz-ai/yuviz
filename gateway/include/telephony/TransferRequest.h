#pragma once

#include <string>

namespace voiceai {

// Arguments for one EslClient::transfer() (cold) attempt.
struct TransferRequest {
    std::string call_id;        // FreeSWITCH channel UUID being transferred
    std::string transfer_type;  // "warm" | "cold" | "none"; logging only
    std::string destination;    // phone number/extension, or a sip:/sips: URI
    std::string reason;
    std::string transfer_id{};  // observability-only correlation id (may be empty)
};

} // namespace voiceai
