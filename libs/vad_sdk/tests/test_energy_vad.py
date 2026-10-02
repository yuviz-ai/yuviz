"""EnergyVAD state-machine tests (pure math)."""

from __future__ import annotations

import math
import struct

from libs.vad_sdk.vad import EnergyVAD, EnergyVADConfig, VADEvent

_FRAME_SAMPLES = 320  # 20ms @ 16kHz, this module's default frame_ms


def _pcm16_frame(amplitude: float, sample_count: int = _FRAME_SAMPLES) -> bytes:
    """amplitude in [0, 1]: 1.0 is ~0dB, 0.0 is the -96dB floor."""
    value = int(amplitude * 32767)
    return struct.pack(f"<{sample_count}h", *([value] * sample_count))


def test_silence_never_triggers_speech_start():
    vad = EnergyVAD()
    for _ in range(50):
        assert vad.process(_pcm16_frame(0.0)) == VADEvent.NONE
    assert vad.last_energy_db == -96.0


def test_sustained_loud_audio_triggers_speech_start_after_onset_window():
    # onset_ms=100, frame_ms=20 -> 5 frames needed.
    cfg = EnergyVADConfig()
    vad = EnergyVAD(cfg)
    loud = _pcm16_frame(1.0)  # ~0dB, well above -35dB threshold

    events = [vad.process(loud) for _ in range(5)]
    assert events[:4] == [VADEvent.NONE] * 4
    assert events[4] == VADEvent.SPEECH_START


def test_a_single_loud_blip_is_not_enough_to_trigger_onset():
    vad = EnergyVAD()
    assert vad.process(_pcm16_frame(1.0)) == VADEvent.NONE
    assert vad.process(_pcm16_frame(0.0)) == VADEvent.NONE
    # The quiet frame resets the onset streak.
    for _ in range(4):
        assert vad.process(_pcm16_frame(1.0)) == VADEvent.NONE


def test_speech_end_after_sustained_silence():
    cfg = EnergyVADConfig(hold_ms=100)  # 5 frames of silence to end
    vad = EnergyVAD(cfg)
    for _ in range(5):
        vad.process(_pcm16_frame(1.0))
    assert vad._in_speech is True  # onset confirmed

    events = [vad.process(_pcm16_frame(0.0)) for _ in range(5)]
    assert events[:4] == [VADEvent.NONE] * 4
    assert events[4] == VADEvent.SPEECH_END


def test_speech_duration_ms_tracks_ongoing_speech_and_resets_after_end():
    cfg = EnergyVADConfig(hold_ms=100)
    vad = EnergyVAD(cfg)
    assert vad.speech_duration_ms == 0

    for _ in range(5):
        vad.process(_pcm16_frame(1.0))
    assert vad.speech_duration_ms == 100  # 5 frames * 20ms

    for _ in range(5):
        vad.process(_pcm16_frame(0.0))
    assert vad.speech_duration_ms == 0


def test_reset_clears_all_state():
    vad = EnergyVAD()
    for _ in range(5):
        vad.process(_pcm16_frame(1.0))
    assert vad._in_speech is True

    vad.reset()

    assert vad._in_speech is False
    assert vad.last_energy_db == -96.0
    assert vad.speech_duration_ms == 0


def test_empty_frame_is_a_no_op():
    vad = EnergyVAD()
    assert vad.process(b"") == VADEvent.NONE


def test_compute_energy_db_matches_a_hand_computed_rms():
    # Full-scale square wave alternating +/-1 has RMS = 1.0 -> 0dB exactly.
    frame = struct.pack("<4h", 32767, -32768, 32767, -32768)
    db = EnergyVAD._compute_energy_db(frame, 4)
    assert math.isclose(db, 0.0, abs_tol=0.01)
