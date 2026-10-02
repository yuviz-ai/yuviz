#pragma once

#include <array>
#include <cstdint>
#include <cstring>
#include <string_view>
#include <vector>

namespace voiceai {

enum class AudioCodec : uint8_t {
    PCM_S16LE = 0,  // 16-bit signed little-endian — native from mod_audio_fork
};

enum class AudioDirection : uint8_t {
    Inbound  = 0,   // FreeSWITCH → Gateway → ConversationService
    Outbound = 1,   // ConversationService → Gateway → FreeSWITCH
};

// Owned audio packet (payload is always a copy).
// IDs are fixed char arrays to avoid a heap allocation per TTS chunk.
struct AudioFrame {
    std::array<char, 37> session_id{};  // null-terminated; 36 chars for UUID
    std::array<char, 37> trace_id{};

    void set_session_id(std::string_view sv) noexcept {
        const auto n = std::min(sv.size(), session_id.size() - 1);
        std::memcpy(session_id.data(), sv.data(), n);
        session_id[n] = '\0';
    }
    void set_trace_id(std::string_view sv) noexcept {
        const auto n = std::min(sv.size(), trace_id.size() - 1);
        std::memcpy(trace_id.data(), sv.data(), n);
        trace_id[n] = '\0';
    }
    [[nodiscard]] std::string_view get_session_id() const noexcept {
        return {session_id.data()};
    }
    [[nodiscard]] std::string_view get_trace_id() const noexcept {
        return {trace_id.data()};
    }

    uint64_t       sequence_num{0};
    uint64_t       timestamp_us{0};
    uint32_t       sample_rate{16'000};
    uint8_t        channels{1};
    AudioCodec     codec{AudioCodec::PCM_S16LE};
    AudioDirection direction{AudioDirection::Inbound};
    bool           is_final{false};  // last TTS frame of a response turn
    std::vector<uint8_t> payload;   // owns the PCM bytes

    [[nodiscard]] size_t sample_count() const noexcept {
        return payload.size() / sizeof(int16_t);
    }
    [[nodiscard]] double duration_ms() const noexcept {
        if (sample_rate == 0 || channels == 0) return 0.0;
        return static_cast<double>(sample_count())
             / (static_cast<double>(sample_rate) * channels) * 1000.0;
    }
    [[nodiscard]] const int16_t* samples() const noexcept {
        return reinterpret_cast<const int16_t*>(payload.data());
    }
    [[nodiscard]] bool is_pcm() const noexcept {
        return codec == AudioCodec::PCM_S16LE;
    }
};

} // namespace voiceai
