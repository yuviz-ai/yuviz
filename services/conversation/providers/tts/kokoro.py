"""
KokoroTTS — local neural TTS (24 kHz output, resampled to the requested rate).

Requires Python <= 3.12: kokoro's spacy/blis dependency doesn't build on 3.13+.
"""

from __future__ import annotations

import asyncio
import logging
import math

import numpy as np

log = logging.getLogger(__name__)

_KOKORO_NATIVE_RATE = 24_000  # Hz — kokoro's output sample rate


class KokoroTTS:
    """
    ITTS implementation backed by kokoro (KPipeline).

    voice       — kokoro voice name (e.g. "af_sarah", "am_adam", "bf_emma")
    speed       — speech rate multiplier (1.0 = normal)
    lang_code   — language code passed to KPipeline ('a' = American English)
    """

    def __init__(
        self,
        voice:     str = "af_sarah",
        speed:     float = 1.0,
        lang_code: str = "a",
    ) -> None:
        from kokoro import KPipeline
        log.info("Loading Kokoro TTS voice=%s lang=%s", voice, lang_code)
        self._pipeline = KPipeline(lang_code=lang_code)
        self._voice    = voice
        self._speed    = speed
        self._lock     = asyncio.Lock()  # KPipeline is not thread-safe for concurrent calls
        log.info("Kokoro TTS ready")

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        if not text.strip():
            return b""
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, self._synthesize_sync, text, sample_rate)

    async def synthesize_stream(self, text: str, sample_rate: int):
        audio = await self.synthesize(text, sample_rate)
        if audio:
            yield audio

    def _synthesize_sync(self, text: str, sample_rate: int) -> bytes:
        chunks: list[np.ndarray] = []

        for _gs, _ps, audio in self._pipeline(
            text, voice=self._voice, speed=self._speed
        ):
            if audio is not None and len(audio) > 0:
                chunks.append(audio)

        if not chunks:
            return b""

        pcm_f32 = np.concatenate(chunks)

        # Resample from Kokoro's native 24 kHz to the requested sample_rate.
        if sample_rate != _KOKORO_NATIVE_RATE:
            pcm_f32 = self._resample(pcm_f32, _KOKORO_NATIVE_RATE, sample_rate)

        # Convert float32 [-1, 1] → int16 L16 PCM.
        pcm_i16 = np.clip(pcm_f32 * 32767, -32768, 32767).astype(np.int16)
        return pcm_i16.tobytes()

    @staticmethod
    def _resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        from scipy.signal import resample_poly
        gcd = math.gcd(from_rate, to_rate)
        return resample_poly(audio, to_rate // gcd, from_rate // gcd).astype(np.float32)
