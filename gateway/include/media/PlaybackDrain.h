#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "media/PlaybackQueue.h"
#include "websocket/IWebSocketConnection.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <thread>

namespace voiceai {

// Drains PlaybackQueue on a dedicated thread and sends PCM to mod_audio_fork.
class PlaybackDrain : private NonCopyable, private NonMovable {
public:
    using Clock = std::function<std::chrono::steady_clock::time_point()>;

    // `now` is the pacing clock; tests inject one to control played_bytes().
    PlaybackDrain(PlaybackQueue&        queue,
                  IWebSocketConnection& connection,
                  Logger&               logger,
                  Clock                 now = [] { return std::chrono::steady_clock::now(); });
    ~PlaybackDrain();

    void start();
    void stop();

    // Audio bytes handed to the connection so far (end-of-response markers excluded).
    uint64_t sent_bytes() const { return sent_bytes_.load(std::memory_order_acquire); }

    // sent_bytes() minus the audio still ahead of the wall clock (the pacing
    // lead FreeSWITCH has not played yet); clamped to [0, sent_bytes()].
    uint64_t played_bytes() const;

    // The calculation behind played_bytes(): `ahead_ns` of audio at
    // `bytes_per_sec` is still queued in FreeSWITCH; <= 0 means all of it played.
    static uint64_t played_bytes_at(uint64_t sent, int64_t ahead_ns, uint64_t bytes_per_sec);

private:
    void loop() noexcept;

    PlaybackQueue&         queue_;
    IWebSocketConnection&  connection_;
    Logger&                logger_;
    Clock                  now_;
    std::atomic<bool>      running_{false};
    std::atomic<uint64_t>  sent_bytes_{0};
    // Pacing state published by loop(): when the last sent frame finishes
    // playing (steady_clock ns since epoch) and the byte rate of that audio.
    std::atomic<int64_t>   playout_end_ns_{0};
    std::atomic<uint64_t>  bytes_per_sec_{0};
    std::thread            thread_;
};

} // namespace voiceai
