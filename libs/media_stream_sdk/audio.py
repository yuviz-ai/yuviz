"""Mu-law 8kHz <-> PCM16 16kHz conversion for Media Stream providers.

Uses stdlib audioop (deprecated in 3.13; this project runs 3.11).
"""

from __future__ import annotations

import audioop
import base64

VOBIZ_SAMPLE_RATE = 8000
PIPELINE_SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2  # 16-bit


class AudioBridge:
    """One per call: keeps ratecv state per direction so chunk boundaries don't click."""

    def __init__(self) -> None:
        self._in_state = None   # provider (8k) -> pipeline (16k)
        self._out_state = None  # pipeline (16k) -> provider (8k)

    def to_pcm16(self, payload_b64: str) -> bytes:
        """Base64 mu-law @ 8kHz -> raw PCM16 @ 16kHz."""
        ulaw = base64.b64decode(payload_b64)
        pcm_8k = audioop.ulaw2lin(ulaw, _SAMPLE_WIDTH)
        pcm_16k, self._in_state = audioop.ratecv(
            pcm_8k, _SAMPLE_WIDTH, 1, VOBIZ_SAMPLE_RATE, PIPELINE_SAMPLE_RATE, self._in_state,
        )
        return pcm_16k

    def from_pcm16(self, pcm_16k: bytes) -> bytes:
        """Raw PCM16 @ 16kHz -> raw mu-law @ 8kHz (not base64; the pacer frames it)."""
        pcm_8k, self._out_state = audioop.ratecv(
            pcm_16k, _SAMPLE_WIDTH, 1, PIPELINE_SAMPLE_RATE, VOBIZ_SAMPLE_RATE, self._out_state,
        )
        return audioop.lin2ulaw(pcm_8k, _SAMPLE_WIDTH)
