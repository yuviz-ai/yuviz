#pragma once

#include <cstdint>
#include <string>

namespace voiceai {

struct SileroVADConfig {
    std::string model_path{"models/silero_vad.onnx"};
    float    speech_threshold{0.5f};   // window probability >= this → speech
    float    silence_threshold{0.35f}; // window probability <  this → silence
    uint32_t onset_ms{96};             // sustained speech before SpeechStart
    uint32_t hold_ms{1000};            // sustained silence before SpeechEnd; must
                                       // exceed natural mid-sentence pauses (0.8-1.2 s)
};

} // namespace voiceai
