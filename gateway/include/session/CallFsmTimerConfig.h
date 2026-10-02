#pragma once

#include <chrono>

namespace voiceai {

struct CallFsmTimerConfig {
    std::chrono::milliseconds connection_timeout{10'000};
    // Per-tenant via Redis "no_speech_timeout_ms"; out-of-range falls back to default.
    static constexpr std::chrono::milliseconds no_speech_timeout_min    {5'000};
    static constexpr std::chrono::milliseconds no_speech_timeout_default{30'000};
    static constexpr std::chrono::milliseconds no_speech_timeout_max    {120'000};
    std::chrono::milliseconds no_speech_timeout {no_speech_timeout_default};
    // Safety net for a stuck-open mic before speech_ended; not a UX limit.
    std::chrono::milliseconds max_utterance_timeout {45'000};
    // STT response time after speech_ended.
    std::chrono::milliseconds stt_timeout        {8'000};
    std::chrono::milliseconds llm_timeout       {20'000};
    std::chrono::milliseconds tts_timeout       {10'000};
    std::chrono::milliseconds playback_timeout  {60'000};
    // Window after the goodbye plays for the caller to speak and cancel the hangup.
    std::chrono::milliseconds goodbye_timeout   {2'500};
    // Speech must persist this long during WaitingForHangup to cancel the hangup,
    // so noise blips don't; a blip restores the full goodbye_timeout.
    std::chrono::milliseconds goodbye_confirm     {300};
    std::chrono::milliseconds barge_in_window     {500};
    // Must exceed the dialplan ring timeout (30s) plus SIP setup. Per-tenant via
    // Redis "transfer_timeout_ms"; out-of-range falls back to default.
    static constexpr std::chrono::milliseconds transfer_timeout_min    {10'000};
    static constexpr std::chrono::milliseconds transfer_timeout_default{45'000};
    static constexpr std::chrono::milliseconds transfer_timeout_max   {120'000};
    std::chrono::milliseconds transfer_timeout  {transfer_timeout_default};
    // Fallback if ConversationFinalized never arrives; covers an LLM summary call.
    std::chrono::milliseconds finalizing_timeout {15'000};
    std::chrono::milliseconds close_timeout      {5'000};
};

} // namespace voiceai
