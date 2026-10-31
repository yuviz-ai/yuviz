#include <gtest/gtest.h>
#include "media/PlaybackDrain.h"
#include "media/PlaybackQueue.h"
#include "logging/Logger.h"

#include <atomic>
#include <chrono>
#include <thread>

namespace {

using namespace voiceai;

class StubMetrics final : public IMetrics {
public:
    bool initialize() override { return true; }
    bool start()      override { return true; }
    void stop()       override {}
    void shutdown()   override {}
    void increment(const std::string&, double = 1.0) override {}
    void gauge(const std::string&, double)           override {}
    void observe(const std::string&, double)         override {}
};

class StubConnection final : public IWebSocketConnection {
public:
    const std::string& id()   const noexcept override { return id_; }
    const std::string& path() const noexcept override { return id_; }
    bool is_open()            const noexcept override { return true; }
    void send_text(const std::string&)           override {}
    void send_binary(const uint8_t*, size_t n)   override { ++sends; wire_bytes += n; }
    void close()                                 override {}
    void set_on_binary(BinaryHandler) override {}
    void set_on_text(TextHandler)     override {}
    void set_on_close(CloseHandler)   override {}

    std::atomic<int>      sends{0};
    std::atomic<uint64_t> wire_bytes{0};
private:
    std::string id_{"conn"};
};

AudioFrame make_frame(size_t bytes) {
    AudioFrame f;
    f.set_session_id("s1");
    f.direction = AudioDirection::Outbound;
    f.payload.assign(bytes, 0);
    return f;
}

TEST(PlaybackDrainTest, SentBytesCountsAudioAndSkipsEndMarkers) {
    StubMetrics   metrics;
    Logger        logger = Logger::make_null();
    PlaybackQueue queue{"s1", 10, metrics, logger};
    StubConnection conn;
    PlaybackDrain drain{queue, conn, logger};

    EXPECT_EQ(drain.sent_bytes(), 0u);
    drain.start();
    queue.push(make_frame(640));
    queue.push(make_frame(320));
    queue.push(make_frame(0));  // end-of-response marker

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds{2};
    while (conn.sends.load() < 2 && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds{5});
    // The marker is popped after the second audio frame; give it time to be skipped.
    std::this_thread::sleep_for(std::chrono::milliseconds{100});
    drain.stop();

    EXPECT_EQ(conn.sends.load(), 2);
    EXPECT_EQ(drain.sent_bytes(), 960u);
    EXPECT_EQ(drain.sent_bytes(), conn.wire_bytes.load());
}

// 16 kHz mono S16LE = 32000 bytes/s.
TEST(PlaybackDrainTest, PlayedBytesAtSubtractsLeadStillAheadOfClock) {
    // 100 ms ahead = 3200 bytes not yet played.
    EXPECT_EQ(PlaybackDrain::played_bytes_at(10'000, 100'000'000, 32'000), 6'800u);
}

TEST(PlaybackDrainTest, PlayedBytesAtEqualsSentOnceLeadHasElapsed) {
    EXPECT_EQ(PlaybackDrain::played_bytes_at(10'000, 0, 32'000), 10'000u);
    EXPECT_EQ(PlaybackDrain::played_bytes_at(10'000, -5'000'000, 32'000), 10'000u);
}

TEST(PlaybackDrainTest, PlayedBytesAtClampsToZeroWhenAheadExceedsSent) {
    EXPECT_EQ(PlaybackDrain::played_bytes_at(640, 100'000'000, 32'000), 0u);
    EXPECT_EQ(PlaybackDrain::played_bytes_at(0, 100'000'000, 32'000), 0u);
}

TEST(PlaybackDrainTest, PlayedBytesTracksInjectedClockAfterSend) {
    StubMetrics   metrics;
    Logger        logger = Logger::make_null();
    PlaybackQueue queue{"s1", 10, metrics, logger};
    StubConnection conn;

    using TimePoint = std::chrono::steady_clock::time_point;
    const TimePoint t0 = std::chrono::steady_clock::now();
    std::atomic<int64_t> clock_ns{t0.time_since_epoch().count()};
    PlaybackDrain drain{queue, conn, logger, [&clock_ns] {
        return TimePoint{std::chrono::steady_clock::duration{clock_ns.load()}};
    }};
    const auto advance_to = [&](std::chrono::milliseconds ms) {
        clock_ns.store((t0 + ms).time_since_epoch().count());
    };

    drain.start();
    queue.push(make_frame(640));  // 20 ms each at 16 kHz mono S16LE
    queue.push(make_frame(640));

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds{2};
    while (drain.sent_bytes() < 1280 && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds{1});
    ASSERT_EQ(drain.sent_bytes(), 1280u);

    // The clock has not moved: both frames (40 ms) are still ahead of it.
    EXPECT_EQ(drain.played_bytes(), 0u);
    advance_to(std::chrono::milliseconds{30});  // 10 ms (320 bytes) still ahead
    EXPECT_EQ(drain.played_bytes(), 960u);
    advance_to(std::chrono::milliseconds{40});
    EXPECT_EQ(drain.played_bytes(), 1280u);
    drain.stop();
}

} // namespace
