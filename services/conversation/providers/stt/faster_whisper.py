"""
FasterWhisperSTT — local transcription using faster-whisper (CTranslate2 backend).

Loads the model once at construction; transcribe() is CPU/GPU-bound and runs
in a thread-pool executor so it does not block the asyncio event loop.

pip install faster-whisper
"""

from __future__ import annotations

import asyncio
import logging
import numpy as np

from ..interfaces import SttResult

log = logging.getLogger(__name__)


class FasterWhisperSTT:
    """
    ISTT implementation backed by faster-whisper.

    model_size  — "tiny", "base", "small", "medium", "large-v3"
    device      — "cpu" | "cuda" | "auto"
    compute_type — "int8" (CPU) | "float16" (GPU) | "float32"
    language    — ISO-639-1 code, e.g. "en".  None = auto-detect.
    """

    def __init__(
        self,
        model_size:   str = "small",
        device:       str = "cpu",
        compute_type: str = "int8",
        language:     str | None = "en",
        beam_size:    int = 2,
    ) -> None:
        # Import eagerly so a missing package fails at startup, not at first call.
        from faster_whisper import WhisperModel
        self._WhisperModel  = WhisperModel
        self._model_size    = model_size
        self._device        = device
        self._compute_type  = compute_type
        self._language      = language
        self._beam_size     = beam_size
        self._model         = None   # populated by load()
        self._lock          = asyncio.Lock()

    async def load(self) -> None:
        """Load the model in a thread-pool executor; call after server.start()."""
        log.info("Loading FasterWhisper model=%s device=%s compute=%s",
                 self._model_size, self._device, self._compute_type)
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(
            None,
            lambda: self._WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
                # Skip huggingface_hub's per-load revision check (latency; breaks air-gapped hosts).
                local_files_only=True,
            ),
        )
        log.info("FasterWhisper ready")

    async def transcribe(self, audio: bytes, sample_rate: int) -> SttResult:
        if not audio or self._model is None:
            return SttResult(text="")

        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._transcribe_sync, audio, sample_rate)

    async def feed_stream(self, session_id: str, chunk: bytes, sample_rate: int) -> None:
        # No incremental decode; finalize_stream() gets the full buffer.
        return

    async def finalize_stream(self, session_id: str, audio: bytes, sample_rate: int) -> SttResult:
        return await self.transcribe(audio, sample_rate)

    async def cancel_stream(self, session_id: str) -> None:
        return

    def _transcribe_sync(self, audio: bytes, sample_rate: int) -> SttResult:
        # Convert raw L16 PCM bytes → float32 numpy array in [-1, 1].
        pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        # FasterWhisper expects 16 kHz mono float32.  Resample if needed.
        if sample_rate != 16_000:
            pcm = self._resample(pcm, sample_rate, 16_000)

        segments, info = self._model.transcribe(
            pcm,
            beam_size=self._beam_size,
            language=self._language,
            vad_filter=False,   # Gateway EnergyVAD already gates speech; double-VAD
                                # strips short utterances and non-speech test tones.
            condition_on_previous_text=False,  # prevents hallucination loops
                                               # ("Bye. Bye. Bye. ...") on noisy audio
        )

        # Drop segments Whisper itself flags as probably-not-speech.  Echo and
        # line noise otherwise decode as polite filler ("Thank you very much.").
        kept: list[str] = []
        for seg in segments:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
                log.debug(
                    "FasterWhisper dropped segment %r no_speech_prob=%.2f avg_logprob=%.2f",
                    seg.text.strip(), seg.no_speech_prob, seg.avg_logprob,
                )
                continue
            kept.append(seg.text.strip())

        text = " ".join(kept).strip()
        log.debug("FasterWhisper transcript=%r lang=%s", text, info.language)
        return SttResult(text=text, confidence=1.0)

    @staticmethod
    def _resample(pcm: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        import math
        from scipy.signal import resample_poly
        gcd = math.gcd(from_rate, to_rate)
        return resample_poly(pcm, to_rate // gcd, from_rate // gcd).astype(np.float32)
