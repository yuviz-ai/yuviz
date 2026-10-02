#pragma once

#include <condition_variable>
#include <mutex>
#include <optional>
#include <string>

namespace voiceai {

// One-shot rendezvous between the lws callbacks and the pool task waiting for the
// metadata frame. First of {text, close} wins; later fulfill calls are no-ops.
struct PendingMetadata {
    std::mutex                 mutex;
    std::condition_variable    cv;
    bool                       ready{false};
    std::optional<std::string> text;   // nullopt: closed or timed out before arrival

    void fulfill_with_text(const std::string& msg) {
        std::lock_guard lock{mutex};
        if (ready) return;
        text  = msg;
        ready = true;
        cv.notify_all();
    }

    void fulfill_with_close() {
        std::lock_guard lock{mutex};
        if (ready) return;
        text  = std::nullopt;
        ready = true;
        cv.notify_all();
    }

    // Blocks up to `timeout`; nullopt if closed or timed out.
    template <typename Rep, typename Period>
    [[nodiscard]] std::optional<std::string> wait_for(
        std::chrono::duration<Rep, Period> timeout) {
        std::unique_lock lock{mutex};
        cv.wait_for(lock, timeout, [&] { return ready; });
        return text;
    }
};

} // namespace voiceai
