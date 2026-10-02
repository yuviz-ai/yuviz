"""Python port of the Gateway's SileroVAD (Silero v6 ONNX, 16kHz).

Unlike EnergyVAD it classifies speech rather than loudness, so line noise/echo doesn't trigger it.
process() takes exactly one 512-sample (32ms) window; LSTM state carries across windows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort

from .vad import VADEvent

log = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "silero_vad.onnx"
WINDOW_SAMPLES = 512   # 32ms @ 16kHz — Silero's fixed window size
WINDOW_BYTES = WINDOW_SAMPLES * 2  # 16-bit PCM
_CONTEXT_SAMPLES = 64
_WINDOW_MS = 32
_STATE_SHAPE = (1, 1, 128)


@dataclass(frozen=True)
class SileroVADConfig:
    speech_threshold: float = 0.5
    silence_threshold: float = 0.35
    onset_ms: int = 96
    # 500ms and 700ms both split utterances at natural mid-sentence pauses.
    hold_ms: int = 1000


class SileroVAD:
    def __init__(self, cfg: SileroVADConfig | None = None) -> None:
        self._cfg = cfg or SileroVADConfig()
        self._onset_needed = max(1, -(-self._cfg.onset_ms // _WINDOW_MS))  # ceil div
        self._hold_needed = max(1, -(-self._cfg.hold_ms // _WINDOW_MS))

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = 1
        sess_options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(_MODEL_PATH), sess_options=sess_options, providers=["CPUExecutionProvider"],
        )

        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)

        self._in_speech = False
        self._onset_windows = 0
        self._silence_windows = 0
        self._speech_windows = 0
        self._fail_count = 0
        self.last_speech_prob = 0.0

    def process(self, pcm16_window: bytes) -> VADEvent:
        if len(pcm16_window) != WINDOW_BYTES:
            log.warning("SileroVAD: expected exactly %d bytes, got %d — dropping window", WINDOW_BYTES, len(pcm16_window))
            return VADEvent.NONE

        samples = np.frombuffer(pcm16_window, dtype=np.int16).astype(np.float32) / 32768.0
        model_input = np.concatenate([self._context, samples]).reshape(1, -1).astype(np.float32)
        self._context = samples[-_CONTEXT_SAMPLES:]

        try:
            prob, self._h, self._c = self._session.run(
                ["speech_probs", "hn", "cn"],
                {"input": model_input, "h": self._h, "c": self._c},
            )
        except Exception:
            self._fail_count += 1
            if self._fail_count % 100 == 1:
                log.exception("SileroVAD: inference failed (count=%d)", self._fail_count)
            return VADEvent.NONE

        self.last_speech_prob = float(np.asarray(prob).reshape(-1)[0])

        if not self._in_speech:
            if self.last_speech_prob >= self._cfg.speech_threshold:
                self._onset_windows += 1
                if self._onset_windows >= self._onset_needed:
                    self._in_speech = True
                    self._silence_windows = 0
                    self._speech_windows = self._onset_windows
                    self._onset_windows = 0
                    return VADEvent.SPEECH_START
            else:
                self._onset_windows = 0
            return VADEvent.NONE

        if self.last_speech_prob < self._cfg.silence_threshold:
            self._silence_windows += 1
            if self._silence_windows >= self._hold_needed:
                self._in_speech = False
                self._silence_windows = 0
                self._speech_windows = 0
                return VADEvent.SPEECH_END
        else:
            self._silence_windows = 0
            self._speech_windows += 1
        return VADEvent.NONE

    @property
    def speech_duration_ms(self) -> int:
        return self._speech_windows * _WINDOW_MS if self._in_speech else 0

    def reset(self) -> None:
        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._in_speech = False
        self._onset_windows = 0
        self._silence_windows = 0
        self._speech_windows = 0
        self.last_speech_prob = 0.0
