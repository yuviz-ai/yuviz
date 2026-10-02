#pragma once

// Gateway-side event vocabulary. Events carry metadata only, never raw PCM.

#include "observability/ObservabilityContext.h"
#include "session/CallFSM.h"

#include <chrono>
#include <cstdint>
#include <string>
#include <variant>

namespace voiceai {

// ── Base header carried by every event ───────────────────────────────────────

struct EventHeader {
    std::string  session_id;
    std::string  tenant_id;
    std::string  trace_id;
    std::string  call_id;
    uint64_t     sequence_num{0};
    std::chrono::steady_clock::time_point timestamp{};

    static EventHeader from(const ObservabilityContext& ctx, uint64_t seq = 0) {
        return {ctx.session_id, ctx.tenant_id, ctx.trace_id, ctx.call_id, seq};
    }
};

// ── Lifecycle ─────────────────────────────────────────────────────────────────

struct SessionCreatedEvent {
    EventHeader hdr;
    std::string caller_did;
    std::string called_did;
};

struct SessionConnectedEvent {
    EventHeader hdr;   // fired when ConversationService sends ServiceReady
};

struct SessionEndedEvent {
    EventHeader hdr;
    std::string reason;
    double      duration_ms{0};
};

// ── Speech (produced by EnergyVAD) ───────────────────────────────────────────

struct SpeechStartedEvent {
    EventHeader hdr;
    float       energy_db{0};
};

struct SpeechEndedEvent {
    EventHeader hdr;
    uint32_t    duration_ms{0};
    float       energy_db{0};
};

// ── Playback (produced by PlaybackQueue) ─────────────────────────────────────

struct PlaybackStartedEvent {
    EventHeader hdr;
};

struct PlaybackFinishedEvent {
    EventHeader hdr;
    double      duration_ms{0};
};

struct PlaybackCancelledEvent {
    EventHeader hdr;
    std::string reason;   // "barge_in" | "session_close"
};

// ── FSM state changes (produced by CallFSM callback) ─────────────────────────

struct SessionStateChangedEvent {
    EventHeader  hdr;
    CallFsmState from_state;
    CallFsmState to_state;
    std::string  trigger;
    double       prev_duration_ms{0};
};

// ── Infrastructure ────────────────────────────────────────────────────────────

struct TimerFiredEvent {
    EventHeader hdr;
    std::string timer_type;   // human-readable; see TimerType.h for enum
};

struct BackpressureEvent {
    EventHeader hdr;
    std::string location;     // "input_ring" | "transport_send" | "playback"
    float       fill_pct{0};
    bool        dropped{false};
};

struct TransportErrorEvent {
    EventHeader hdr;
    std::string error;
    bool        fatal{false};
};

// ── Variant ───────────────────────────────────────────────────────────────────

using SessionEvent = std::variant<
    SessionCreatedEvent,
    SessionConnectedEvent,
    SessionEndedEvent,
    SpeechStartedEvent,
    SpeechEndedEvent,
    PlaybackStartedEvent,
    PlaybackFinishedEvent,
    PlaybackCancelledEvent,
    SessionStateChangedEvent,
    TimerFiredEvent,
    BackpressureEvent,
    TransportErrorEvent
>;

inline const EventHeader& event_header(const SessionEvent& ev) {
    return std::visit([](const auto& e) -> const EventHeader& { return e.hdr; }, ev);
}

} // namespace voiceai
