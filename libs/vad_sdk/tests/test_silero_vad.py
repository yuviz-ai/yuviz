"""SileroVAD tests against the real ONNX model (no speech recording needed)."""

from __future__ import annotations

import struct

from libs.vad_sdk.silero_vad import SileroVAD, SileroVADConfig, WINDOW_BYTES, WINDOW_SAMPLES
from libs.vad_sdk.vad import VADEvent


def _silence_window() -> bytes:
    return struct.pack(f"<{WINDOW_SAMPLES}h", *([0] * WINDOW_SAMPLES))


def test_wrong_size_window_is_dropped_not_crashed():
    vad = SileroVAD()
    assert vad.process(b"\x00" * 10) == VADEvent.NONE
    assert vad.process(b"") == VADEvent.NONE


def test_sustained_silence_never_triggers_speech_start():
    vad = SileroVAD()
    for _ in range(50):
        assert vad.process(_silence_window()) == VADEvent.NONE
    assert vad.last_speech_prob < SileroVADConfig().speech_threshold


def test_reset_clears_state_back_to_construction_defaults():
    vad = SileroVAD()
    for _ in range(10):
        vad.process(_silence_window())
    vad.reset()

    assert vad.last_speech_prob == 0.0
    assert vad.speech_duration_ms == 0
    assert vad._in_speech is False


def test_speech_duration_ms_is_zero_when_not_in_speech():
    vad = SileroVAD()
    assert vad.speech_duration_ms == 0
    vad.process(_silence_window())
    assert vad.speech_duration_ms == 0


def test_window_bytes_matches_window_samples_at_16bit_pcm():
    assert WINDOW_BYTES == WINDOW_SAMPLES * 2
