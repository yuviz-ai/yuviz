#include "media/PlaybackDrain.h"
#include "common/ThreadUtils.h"

#include <chrono>
#include <stdexcept>
#include <utility>

namespace voiceai {

PlaybackDrain::PlaybackDrain(PlaybackQueue&        queue,
                             IWebSocketConnection& connection,
                             Logger&               logger,
                             Clock                 now)
    : queue_(queue), connection_(connection), logger_(logger), now_(std::move(now))
{}

PlaybackDrain::~PlaybackDrain() { stop(); }

void PlaybackDrain::start() {
    logger_.debug("PlaybackDrain starting");
    running_.store(true, std::memory_order_relaxed);
    thread_ = std::thread([this] {
        set_thread_name("PlaybackDrain");
        loop();
    });
}

void PlaybackDrain::stop() {
    if (!running_.exchange(false, std::memory_order_acq_rel)) return;
    logger_.debug("PlaybackDrain stopping");
    queue_.stop();
    if (thread_.joinable()) thread_.join();
}

uint64_t PlaybackDrain::played_bytes_at(uint64_t sent, int64_t ahead_ns, uint64_t bytes_per_sec) {
    if (ahead_ns <= 0) return sent;

    const uint64_t ahead = static_cast<uint64_t>(ahead_ns) * bytes_per_sec / 1'000'000'000;
    return ahead >= sent ? 0 : sent - ahead;
}

uint64_t PlaybackDrain::played_bytes() const {
    const int64_t now_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        now_().time_since_epoch()).count();
    // Read sent first: the acquire pairs with the release in loop(), which stored
    // the pacing before it added these bytes.
    const uint64_t sent = sent_bytes();
    return played_bytes_at(sent,
                           playout_end_ns_.load(std::memory_order_relaxed) - now_ns,
                           bytes_per_sec_.load(std::memory_order_relaxed));
}

void PlaybackDrain::loop() noexcept {
    // Pace at real-time playout, keeping mod_audio_fork at most ~kLead deep.
    // Audio already sent to FreeSWITCH cannot be recalled, so kLead bounds
    // barge-in stop latency; the rest stays here where cancel can clear it.
    constexpr auto kLead = std::chrono::milliseconds{100};

    auto next_playout = now_();

    while (running_.load(std::memory_order_acquire)) {
        try {
            auto frame = queue_.pop(std::chrono::milliseconds{50});
            if (!frame) {
                next_playout = now_();  // idle — reset pacing baseline
                continue;
            }

            const auto now = now_();
            if (next_playout < now) next_playout = now;
            if (next_playout - now > kLead)
                std::this_thread::sleep_until(next_playout - kLead);

            const auto duration_us = static_cast<int64_t>(frame->duration_ms() * 1000.0);
            next_playout += std::chrono::microseconds{duration_us};

            // Publish pacing before the bytes it describes: played_bytes() reads
            // sent_bytes_ (acquire) first, so it never sees new bytes with old pacing.
            if (duration_us > 0)
                bytes_per_sec_.store(frame->payload.size() * 1'000'000 / static_cast<uint64_t>(duration_us),
                                     std::memory_order_relaxed);
            playout_end_ns_.store(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    next_playout.time_since_epoch()).count(),
                std::memory_order_relaxed);

            // Empty payload = end-of-response marker; nothing to send.
            if (!frame->payload.empty() && connection_.is_open()) {
                connection_.send_binary(frame->payload.data(), frame->payload.size());
                sent_bytes_.fetch_add(frame->payload.size(), std::memory_order_release);
            }
        } catch (const std::exception& e) {
            logger_.error("PlaybackDrain: send exception what={}", e.what());
        } catch (...) {
            logger_.error("PlaybackDrain: send unknown exception");
        }
    }
}

} // namespace voiceai
