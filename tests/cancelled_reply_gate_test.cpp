#include <gtest/gtest.h>
#include "session/CancelledReplyGate.h"

namespace {

using namespace voiceai;
using std::chrono::milliseconds;

const CancelledReplyGate::TimePoint t0{std::chrono::seconds{100}};

TEST(CancelledReplyGateTest, KeepsAudioWhenNothingWasCancelled) {
    CancelledReplyGate gate;
    EXPECT_FALSE(gate.should_drop_audio(t0));
}

TEST(CancelledReplyGateTest, DropsAudioBetweenCancelAndAckThenKeepsNextReply) {
    CancelledReplyGate gate;
    gate.on_cancel_sent(1, t0);
    EXPECT_TRUE(gate.should_drop_audio(t0 + milliseconds{10}));
    gate.on_cancel_resolved(1);
    EXPECT_FALSE(gate.should_drop_audio(t0 + milliseconds{20}));
}

TEST(CancelledReplyGateTest, StaysClosedUntilEveryCancelIsAcked) {
    CancelledReplyGate gate;
    gate.on_cancel_sent(1, t0);
    gate.on_cancel_sent(2, t0 + milliseconds{50});
    gate.on_cancel_resolved(1);
    EXPECT_TRUE(gate.should_drop_audio(t0 + milliseconds{60}));
    gate.on_cancel_resolved(2);
    EXPECT_FALSE(gate.should_drop_audio(t0 + milliseconds{70}));
}

TEST(CancelledReplyGateTest, UnmatchedAckDoesNotDropLaterAudioOrOpenTheGate) {
    CancelledReplyGate gate;
    gate.on_cancel_resolved(9);
    EXPECT_FALSE(gate.should_drop_audio(t0));
    gate.on_cancel_sent(1, t0);
    gate.on_cancel_resolved(9);
    EXPECT_TRUE(gate.should_drop_audio(t0));
}

TEST(CancelledReplyGateTest, AckWithoutIdResolvesOldestCancel) {
    CancelledReplyGate gate;
    gate.on_cancel_sent(1, t0);
    gate.on_cancel_sent(2, t0 + milliseconds{10});
    gate.on_cancel_resolved(std::nullopt);
    EXPECT_TRUE(gate.should_drop_audio(t0 + milliseconds{20}));
    gate.on_cancel_resolved(std::nullopt);
    EXPECT_FALSE(gate.should_drop_audio(t0 + milliseconds{20}));
    gate.on_cancel_resolved(std::nullopt);  // nothing outstanding: harmless
    EXPECT_FALSE(gate.should_drop_audio(t0 + milliseconds{20}));
}

TEST(CancelledReplyGateTest, CancelThatWasNotSentLeavesGateOpen) {
    CancelledReplyGate gate;
    gate.on_cancel_sent(1, t0);
    gate.on_cancel_resolved(1);  // transport reported the send failed
    EXPECT_FALSE(gate.should_drop_audio(t0));
}

TEST(CancelledReplyGateTest, OpensAfterDeadlineWhenAckNeverComes) {
    CancelledReplyGate gate;
    gate.on_cancel_sent(1, t0);
    EXPECT_TRUE(gate.should_drop_audio(t0 + CancelledReplyGate::kMaxClosed - milliseconds{1}));
    EXPECT_FALSE(gate.should_drop_audio(t0 + CancelledReplyGate::kMaxClosed));
}

TEST(CancelledReplyGateTest, LateAckOfExpiredCancelDoesNotOpenGateForNewerCancel) {
    CancelledReplyGate gate;
    gate.on_cancel_sent(1, t0);  // ack arrives too late
    const auto later = t0 + CancelledReplyGate::kMaxClosed + milliseconds{1};
    gate.on_cancel_sent(2, later);
    gate.on_cancel_resolved(1);
    EXPECT_TRUE(gate.should_drop_audio(later));
    gate.on_cancel_resolved(2);
    EXPECT_FALSE(gate.should_drop_audio(later));
}

} // namespace
