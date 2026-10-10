"""
KokoroTTS — local neural TTS (24 kHz output, resampled to the requested rate).

Requires Python <= 3.12: kokoro's spacy/blis dependency doesn't build on 3.13+.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import numpy as np

from libs.config_sdk.languages import LANGUAGES, normalize_language

from ..interfaces import INSTANCE_LANGUAGE

log = logging.getLogger(__name__)

_KOKORO_NATIVE_RATE = 24_000  # Hz — kokoro's output sample rate


def kokoro_lang_code(language: str | None) -> str | None:
    """ISO 639-1 -> KPipeline lang_code (en->a, hi->h, ...); None when Kokoro has no voice for it."""
    lang = LANGUAGES.get(normalize_language(language) or "")
    return lang.kokoro_code if lang else None


def _row_language(lang_code: str) -> str | None:
    """ISO 639-1 language a KPipeline lang_code speaks ('a'/'b' are both English)."""
    if lang_code in ("a", "b"):
        return "en"
    return next((iso for iso, lang in LANGUAGES.items() if lang.kokoro_code == lang_code), None)


class KokoroTTS:
    """
    ITTS implementation backed by kokoro (KPipeline).

    voice       — kokoro voice name (e.g. "af_sarah", "am_adam", "hf_alpha")
    speed       — speech rate multiplier (1.0 = normal)
    lang_code   — KPipeline language code for this row ('a' = American English)

    A per-call `language=` (ISO 639-1) picks another KPipeline, created lazily and
    cached per lang_code. Every pipeline shares the first one's KModel, so the
    weights load once; only the per-language G2P is new.
    """

    accepts_language = True

    def __init__(
        self,
        voice:     str = "af_sarah",
        speed:     float = 1.0,
        lang_code: str = "a",
    ) -> None:
        from kokoro import KPipeline
        log.info("Loading Kokoro TTS voice=%s lang=%s", voice, lang_code)
        self._KPipeline = KPipeline
        self._lang_code = lang_code
        self._pipelines: dict[str, Any] = {lang_code: KPipeline(lang_code=lang_code)}
        self._voice    = voice
        self._speed    = speed
        self._lock     = asyncio.Lock()  # KPipeline is not thread-safe for concurrent calls
        self._warned_languages: set[str] = set()
        # lang_codes whose KPipeline failed to build (e.g. missing G2P extras): never retried.
        self._failed_lang_codes: set[str] = set()
        log.info("Kokoro TTS ready")

    def _resolve_lang_code(self, language: Any) -> str:
        if language is INSTANCE_LANGUAGE or language is None:
            return self._lang_code
        # A Kokoro voice speaks only its own language: a request for another one (e.g. an
        # agent set to en-IN on a Hindi voice) keeps the row's pipeline, as before languages
        # reached Kokoro. Multilingual agents get a matching voice per language (validated).
        voice_language = _row_language(self._voice[:1].lower()) if self._voice else None
        if voice_language is not None and normalize_language(language) != voice_language:
            if language not in self._warned_languages:
                self._warned_languages.add(language)
                log.warning(
                    "Kokoro voice %s can't speak language=%r — using lang_code=%s",
                    self._voice, language, self._lang_code,
                )
            return self._lang_code
        if normalize_language(language) == _row_language(self._lang_code):
            return self._lang_code  # same language as the row: keep its own accent (e.g. British 'b')
        code = kokoro_lang_code(language)
        if code is None:
            if language not in self._warned_languages:
                self._warned_languages.add(language)
                log.warning("Kokoro has no pipeline for language=%r — using lang_code=%s", language, self._lang_code)
            return self._lang_code
        return code

    def _pipeline_sync(self, lang_code: str) -> Any:
        """Get or build the KPipeline for lang_code. Call under self._lock (in the executor).

        A build failure is logged once and falls back to the row's own pipeline: speaking
        with the wrong pronunciation beats silence on every sentence in that language."""
        pipeline = self._pipelines.get(lang_code)
        if pipeline is not None:
            return pipeline
        base = self._pipelines[self._lang_code]
        if lang_code in self._failed_lang_codes:
            return base
        log.info("Kokoro: building pipeline lang_code=%s", lang_code)
        try:
            pipeline = self._KPipeline(lang_code=lang_code, model=base.model)
        except Exception:
            self._failed_lang_codes.add(lang_code)
            log.exception(
                "Kokoro: can't build pipeline lang_code=%s — using lang_code=%s instead",
                lang_code, self._lang_code,
            )
            return base
        self._pipelines[lang_code] = pipeline
        return pipeline

    async def prewarm(self, languages: list[str]) -> None:
        """Build the pipelines for these ISO languages now, so the first sentence
        in a new language doesn't pay the G2P load on the turn's critical path."""
        loop = asyncio.get_running_loop()
        for code in {self._resolve_lang_code(lang) for lang in languages}:
            if code in self._pipelines or code in self._failed_lang_codes:
                continue
            async with self._lock:
                await loop.run_in_executor(None, self._pipeline_sync, code)

    async def synthesize(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> bytes:
        if not text.strip():
            return b""
        lang_code = self._resolve_lang_code(language)
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, self._synthesize_sync, text, sample_rate, lang_code)

    async def synthesize_stream(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE):
        audio = await self.synthesize(text, sample_rate, language=language)
        if audio:
            yield audio

    def _synthesize_sync(self, text: str, sample_rate: int, lang_code: str | None = None) -> bytes:
        chunks: list[np.ndarray] = []

        pipeline = self._pipeline_sync(lang_code or self._lang_code)
        for _gs, _ps, audio in pipeline(
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
