#pragma once

#include "common/AudioFrame.h"
#include "common/IClock.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"
#include "logging/Logger.h"
#include "media/IVAD.h"
#include "media/PlaybackQueue.h"
#include "metrics/IMetrics.h"
#include "utils/RingBuffer.h"

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace voiceai {

// Callbacks fired by MediaSession (invoked on AudioWorker thread unless noted).
struct MediaSessionCallbacks {
    std::function<void(AudioFrame)>                   on_audio_frame;

    std::function<void(float energy_db)>              on_speech_started;
    std::function<void(uint32_t duration_ms,
                       float energy_db)>              on_speech_ended;
    std::function<void()>                             on_playback_finished;
    std::function<void()>                             on_playback_cancelled;
};

// Per-call data plane. SPSC: push_inbound (lws thread) is the only producer,
// drain_available (one AudioWorker thread) the only consumer.
class MediaSession : private NonCopyable, private NonMovable {
public:
    MediaSession(std::string              session_id,
                 uint32_t                 sample_rate,
                 uint8_t                  channels,
                 uint32_t                 ring_buffer_ms,
                 uint32_t                 frame_ms,
                 std::unique_ptr<IVAD>    vad,
                 size_t                   playback_max_frames,
                 IMetrics&                metrics,
                 IClock&                  clock,
                 Logger&                  logger);

    ~MediaSession() = default;

    bool push_inbound(const uint8_t* data, size_t byte_len) noexcept;

    // Drains one frame, runs VAD, fires callbacks. Returns samples consumed.
    size_t drain_available() noexcept;

    // Thread-safe; called from the gRPC receive thread.
    void push_outbound(AudioFrame frame);

    // Barge-in.
    void cancel_playback();

    // Must be called before AudioWorkerPool::assign(); its mutex publishes
    // callbacks_ to the worker. Calling it after races drain_available().
    void set_callbacks(MediaSessionCallbacks cbs);

    [[nodiscard]] const std::string& session_id()    const noexcept { return session_id_; }
    [[nodiscard]] bool               has_inbound()   const noexcept;
    [[nodiscard]] bool               is_playing()    const noexcept { return playback_active_.load(std::memory_order_relaxed); }
    [[nodiscard]] PlaybackQueue&     playback_queue() noexcept { return playback_queue_; }

private:
    std::string           session_id_;
    uint32_t              sample_rate_;
    uint32_t              frame_samples_;
    IMetrics&             metrics_;
    IClock&               clock_;
    Logger&               logger_;

    RingBuffer<int16_t>   ring_;
    std::unique_ptr<IVAD> vad_;
    PlaybackQueue         playback_queue_;

    MediaSessionCallbacks callbacks_;        // written once (set_callbacks) before T2/T4 start

    std::vector<int16_t>  frame_buf_;       // only on AudioWorker thread (T2)
    std::atomic<uint64_t> inbound_seq_{0};  // monotonic per-session frame counter

    // Written by both the gRPC reader and the playback thread.
    std::atomic<bool>     playback_active_{false};

    // Set when the response's final frame is queued; gates on_drained so
    // transient mid-response queue gaps aren't reported as end of playback.
    std::atomic<bool>     final_queued_{false};
};

} // namespace voiceai
