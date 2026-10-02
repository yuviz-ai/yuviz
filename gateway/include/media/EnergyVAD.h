#pragma once

#include "media/EnergyVADConfig.h"
#include "media/IVAD.h"
#include "common/NonCopyable.h"

#include <cstddef>
#include <cstdint>

namespace voiceai {

// Energy-based VAD with two-threshold hysteresis. Hot path: no allocation or syscalls.
class EnergyVAD final : public IVAD, private NonCopyable {
public:
    explicit EnergyVAD(EnergyVADConfig cfg = {}) noexcept;

    [[nodiscard]] VADEvent process(const int16_t* samples,
                                   size_t          count) noexcept override;

    [[nodiscard]] float    last_energy_db()    const noexcept override { return last_energy_db_; }
    [[nodiscard]] uint32_t speech_duration_ms() const noexcept override;

    void reset() noexcept override;

private:
    [[nodiscard]] static float compute_energy_db(const int16_t* samples,
                                                  size_t          count) noexcept;

    EnergyVADConfig cfg_;
    bool            in_speech_{false};
    uint32_t        silence_frames_{0};   // consecutive silent frames since last speech
    uint32_t        speech_frames_{0};    // frames accumulated in current speech segment
    uint32_t        onset_frames_{0};     // consecutive loud frames while not in speech
    float           last_energy_db_{-96.0f};
    uint32_t        hold_frames_{0};      // silence_threshold in frames (derived from cfg)
    uint32_t        onset_needed_{1};     // onset_ms in frames (derived from cfg)
};

} // namespace voiceai
