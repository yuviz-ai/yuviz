#pragma once

#include "common/AudioFrame.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "metrics/IMetrics.h"

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <functional>
#include <mutex>
#include <optional>
#include <queue>
#include <string>

namespace voiceai {

// Bounded outbound TTS queue. Drops newest on overflow: dropping oldest would
// corrupt speech about to play, while this only truncates an over-long tail.
class PlaybackQueue : private NonCopyable, private NonMovable {
public:
    using DrainCallback = std::function<void()>;  // fired when queue becomes empty

    PlaybackQueue(std::string session_id,
                  size_t      max_frames,
                  IMetrics&   metrics,
                  Logger&     logger);

    void push(AudioFrame frame);

    // Returns nullopt on timeout/stop.
    std::optional<AudioFrame> pop(std::chrono::milliseconds timeout = std::chrono::milliseconds{100});

    void clear();

    // Unblocks the consumer.
    void stop();

    void set_on_drained(DrainCallback cb);

    [[nodiscard]] size_t size()     const noexcept;
    [[nodiscard]] bool   is_empty() const noexcept;
    [[nodiscard]] float  fill_pct() const noexcept;

private:
    std::string         session_id_;
    size_t              max_frames_;
    IMetrics&           metrics_;
    Logger&             logger_;

    mutable std::mutex       mutex_;
    std::condition_variable  cv_;
    std::queue<AudioFrame>   queue_;
    bool                     stopped_{false};
    uint64_t                 dropped_{0};   // frames rejected in current overflow episode
    DrainCallback            on_drained_;
};

} // namespace voiceai
