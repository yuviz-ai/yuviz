"""
ElevenLabsTTS — cloud synthesis via ElevenLabs' text-to-speech endpoint.

Only fixed PCM rates are served, so request the nearest rate >= target and downsample.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import numpy as np

from libs.config_sdk.languages import normalize_language

from ..interfaces import INSTANCE_LANGUAGE

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.elevenlabs.io"

# Ascending; pick the smallest >= requested so we only ever downsample.
_SUPPORTED_PCM_RATES = (8000, 16000, 22050, 24000, 44100)


def _supports_language_code(model_id: str) -> bool:
    """Only the *_v2_5 models accept language_code; multilingual_v2 detects the language from the text."""
    return model_id.endswith("_v2_5")


def _nearest_supported_rate(requested: int) -> int:
    for rate in _SUPPORTED_PCM_RATES:
        if rate >= requested:
            return rate
    return _SUPPORTED_PCM_RATES[-1]


class ElevenLabsTTS:
    """ITTS backed by ElevenLabs' /v1/text-to-speech/{voice_id}.

    language_code forces output language on *_v2_5 models (other models ignore it and
    auto-detect from the text); None = auto-detect. A per-call `language=` overrides it (accepts_language).
    """

    accepts_language = True

    def __init__(
        self,
        api_key:       str,
        voice_id:      str,
        model_id:      str = "eleven_turbo_v2_5",
        base_url:      str = _DEFAULT_BASE_URL,
        timeout_s:     float = 15.0,
        speed:         float = 1.0,
        language_code: str | None = None,
    ) -> None:
        self._voice_id      = voice_id
        self._speed         = speed
        self._model_id      = model_id
        self._language_code = language_code
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"xi-api-key": api_key},
            timeout=timeout_s,
        )
        log.info(
            "ElevenLabsTTS voice_id=%s model_id=%s language_code=%s",
            voice_id, model_id, language_code,
        )

    async def synthesize(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE) -> bytes:
        if not text.strip():
            return b""
        language_code = self._language_code if language is INSTANCE_LANGUAGE else language
        # language_code is ISO 639-1; agents.language is often regional ("en-US").
        language_code = normalize_language(language_code)
        if not _supports_language_code(self._model_id):
            language_code = None

        output_rate = _nearest_supported_rate(sample_rate)

        try:
            resp = await self._client.post(
                f"/v1/text-to-speech/{self._voice_id}",
                params={"output_format": f"pcm_{output_rate}"},
                json={
                    "text": text,
                    "model_id": self._model_id,
                    **(
                        {"voice_settings": {"speed": self._speed}}
                        if self._speed != 1.0 else {}
                    ),
                    **({"language_code": language_code} if language_code else {}),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            log.exception("ElevenLabsTTS request failed")
            return b""

        pcm = resp.content
        if output_rate == sample_rate:
            return pcm

        return self._resample(pcm, output_rate, sample_rate)

    async def synthesize_stream(self, text: str, sample_rate: int, *, language: Any = INSTANCE_LANGUAGE):
        audio = await self.synthesize(text, sample_rate, language=language)
        if audio:
            yield audio

    @staticmethod
    def _resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
        import math
        from scipy.signal import resample_poly

        audio_i16 = np.frombuffer(pcm, dtype=np.int16)
        audio_f32 = audio_i16.astype(np.float32) / 32768.0

        gcd = math.gcd(from_rate, to_rate)
        resampled = resample_poly(audio_f32, to_rate // gcd, from_rate // gcd).astype(np.float32)

        return np.clip(resampled * 32767, -32768, 32767).astype(np.int16).tobytes()

    async def aclose(self) -> None:
        await self._client.aclose()
