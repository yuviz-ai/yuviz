"""
DeepgramTTS — cloud synthesis via Deepgram's Aura endpoint.

/v1/speak accepts any linear16 sample rate directly, so no resampling is needed.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

import httpx

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepgram.com"


class DeepgramTTS:
    """ITTS backed by Deepgram's /v1/speak; voice is an Aura model name."""

    def __init__(
        self,
        api_key:   str,
        voice:     str = "aura-asteria-en",
        base_url:  str = _DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
    ) -> None:
        self._voice = voice
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout_s,
        )
        log.info("DeepgramTTS voice=%s", voice)

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        if not text.strip():
            return b""

        try:
            resp = await self._client.post(
                "/v1/speak",
                params={
                    "model": self._voice,
                    "encoding": "linear16",
                    "sample_rate": sample_rate,
                    "container": "none",
                },
                json={"text": text},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            log.exception("DeepgramTTS request failed")
            return b""

        return resp.content

    async def synthesize_stream(self, text: str, sample_rate: int) -> AsyncGenerator[bytes, None]:
        if not text.strip():
            return

        try:
            async with self._client.stream(
                "POST", "/v1/speak",
                params={
                    "model": self._voice,
                    "encoding": "linear16",
                    "sample_rate": sample_rate,
                    "container": "none",
                },
                json={"text": text},
            ) as resp:
                resp.raise_for_status()
                # HTTP chunks can split a 16-bit sample; carry the odd byte over.
                pending = b""
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    data = pending + chunk
                    even_len = len(data) - (len(data) % 2)
                    pending = data[even_len:]
                    if even_len:
                        yield data[:even_len]
        except httpx.HTTPError:
            log.exception("DeepgramTTS streaming request failed")

    async def aclose(self) -> None:
        await self._client.aclose()
