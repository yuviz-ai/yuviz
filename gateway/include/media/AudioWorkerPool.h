#pragma once

#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "media/MediaSession.h"

#include <atomic>
#include <cstddef>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace voiceai {

// Fixed worker pool draining MediaSession ring buffers. Each session is pinned to
// one worker for life (SPSC invariant).
class AudioWorkerPool : private NonCopyable, private NonMovable {
public:
    // Worker idle sleep is frame_ms / 40.
    AudioWorkerPool(size_t worker_count, uint32_t frame_ms, Logger& logger);
    ~AudioWorkerPool();

    bool start();
    void stop();

    // Returns the assigned worker index. Thread-safe.
    size_t assign(MediaSession* session);

    // Blocks until no worker is inside drain_available() for it.
    // Must be called before the MediaSession is destroyed.
    void unassign(MediaSession* session);

    [[nodiscard]] size_t worker_count()  const noexcept { return workers_.size(); }
    [[nodiscard]] size_t session_count() const noexcept {
        return session_count_.load(std::memory_order_relaxed);
    }

private:
    // shared_ptr so worker snapshots stay valid while unassign() erases.
    struct SessionEntry {
        MediaSession*       session;
        // Set under sessions_mutex, checked by the worker under drain_mutex.
        std::atomic<bool>   removed{false};
        // Held during drain_available(); unassign() takes it to wait out a drain.
        std::mutex          drain_mutex;
    };

    struct Worker {
        std::thread                                 thread;
        std::vector<std::shared_ptr<SessionEntry>>  sessions;  // guarded by sessions_mutex
        std::mutex                                  sessions_mutex;
    };

    void worker_loop(size_t idx);

    size_t              worker_count_;
    uint32_t            frame_ms_;
    Logger&             logger_;
    std::atomic<size_t> session_count_{0};
    std::vector<std::unique_ptr<Worker>> workers_;
    std::atomic<bool>   running_{false};
    std::atomic<size_t> round_robin_{0};
};

} // namespace voiceai
