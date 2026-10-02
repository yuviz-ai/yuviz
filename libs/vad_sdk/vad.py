"""Python port of the Gateway's EnergyVAD (amplitude threshold), kept as a fallback.

onset_ms requires sustained energy so a single loud frame (echo blip) doesn't count.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import Enum, auto


class VADEvent(Enum):
    NONE = auto()
    SPEECH_START = auto()
    SPEECH_END = auto()


@dataclass(frozen=True)
class EnergyVADConfig:
    speech_threshold_db: float = -35.0
    silence_threshold_db: float = -40.0
    hold_ms: int = 500
    frame_ms: int = 20
    onset_ms: int = 100


class EnergyVAD:
    def __init__(self, cfg: EnergyVADConfig | None = None) -> None:
        self._cfg = cfg or EnergyVADConfig()
        self._hold_frames = max(1, self._cfg.hold_ms // self._cfg.frame_ms)
        self._onset_needed = max(1, -(-self._cfg.onset_ms // self._cfg.frame_ms))  # ceil div

        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._onset_frames = 0
        self.last_energy_db = -96.0

    def process(self, pcm16_frame: bytes) -> VADEvent:
        """One frame_ms of mono 16-bit PCM (640 bytes at 16kHz/20ms)."""
        sample_count = len(pcm16_frame) // 2
        if sample_count == 0:
            return VADEvent.NONE

        self.last_energy_db = self._compute_energy_db(pcm16_frame, sample_count)

        if not self._in_speech:
            if self.last_energy_db >= self._cfg.speech_threshold_db:
                self._onset_frames += 1
                if self._onset_frames >= self._onset_needed:
                    self._in_speech = True
                    self._silence_frames = 0
                    self._speech_frames = self._onset_frames
                    self._onset_frames = 0
                    return VADEvent.SPEECH_START
            else:
                self._onset_frames = 0
            return VADEvent.NONE

        if self.last_energy_db < self._cfg.silence_threshold_db:
            self._silence_frames += 1
            if self._silence_frames >= self._hold_frames:
                self._in_speech = False
                self._silence_frames = 0
                self._speech_frames = 0
                return VADEvent.SPEECH_END
        else:
            self._silence_frames = 0
            self._speech_frames += 1
        return VADEvent.NONE

    @property
    def speech_duration_ms(self) -> int:
        return self._speech_frames * self._cfg.frame_ms if self._in_speech else 0

    def reset(self) -> None:
        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._onset_frames = 0
        self.last_energy_db = -96.0

    @staticmethod
    def _compute_energy_db(pcm16_frame: bytes, sample_count: int) -> float:
        samples = struct.unpack(f"<{sample_count}h", pcm16_frame[: sample_count * 2])
        total = sum((s / 32768.0) ** 2 for s in samples)
        rms = math.sqrt(total / sample_count)
        if rms < 1e-10:
            return -96.0
        return 20.0 * math.log10(rms)
