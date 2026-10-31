#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <mutex>
#include <optional>

namespace voiceai {

// Drops TTS audio still in flight from a reply the caller barged in on.
//
// The service answers every CancelGeneration with one CancelAck, echoing the
// cancel's id, on the same ordered stream and after it stops yielding that
// reply's chunks. So chunks that arrive between sending a cancel and its ack
// belong to the cancelled reply, and the next reply's chunks (which follow
// the ack) are kept. Acks are matched by id, so a late ack for an old cancel
// cannot open the gate for a newer one. An ack without an id (a service that
// does not echo it) resolves the oldest outstanding cancel.
//
// Fails open: each cancel also expires kMaxClosed after it was sent, so a lost
// ack can never silence the rest of the call.
// The control thread cancels; the transport reader thread acks and checks.
class CancelledReplyGate {
public:
    using Clock     = std::chrono::steady_clock;
    using TimePoint = Clock::time_point;

    static constexpr std::chrono::milliseconds kMaxClosed{3000};

    // Call before sending the cancel: the ack can arrive before the send returns.
    // Ids are issued in increasing order.
    void on_cancel_sent(uint64_t cancel_id, TimePoint now) {
        std::lock_guard lock{mutex_};
        std::erase_if(deadlines_, [now](const auto& kv) { return kv.second <= now; });
        deadlines_[cancel_id] = now + kMaxClosed;
    }

    // CancelAck received, or the cancel turned out not to have been sent.
    void on_cancel_resolved(std::optional<uint64_t> cancel_id) {
        std::lock_guard lock{mutex_};
        if (cancel_id)
            deadlines_.erase(*cancel_id);
        else if (!deadlines_.empty())
            deadlines_.erase(deadlines_.begin());
    }

    bool should_drop_audio(TimePoint now) const {
        std::lock_guard lock{mutex_};
        for (const auto& kv : deadlines_)
            if (now < kv.second) return true;
        return false;
    }

private:
    mutable std::mutex                    mutex_;
    std::map<uint64_t, TimePoint>         deadlines_;  // cancel id -> expiry
};

} // namespace voiceai
