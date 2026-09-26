"""
Mulaw 8kHz <-> PCM16 16kHz conversion, shared by every provider's Media
Stream bridge (Vobiz's Media Streams protocol, Plivo/Twilio-compatible,
confirmed live against the real Vobiz API and Dograh's own working
pipecat.serializers.vobiz.VobizFrameSerializer; Cloudonix speaks the same
Twilio Media Streams wire shape). Both speak base64-encoded mu-law audio
at 8kHz; Conversation Service's gRPC contract requires 16-bit signed PCM
at 16000 Hz (AUDIO_CODEC_PCM_S16LE — see conversation.proto and
services/webcall/__main__.py's own audio contract note).

Uses stdlib audioop rather than a new numpy/scipy dependency — deprecated
since 3.13 but still fully functional under this project's venv (Python
3.11, same interpreter every telephony-adjacent process already runs
under). ratecv's returned state is threaded through per direction per
call so resampling stays continuous across chunks instead of clicking at
every boundary.
"""

from __future__ import annotations

import audioop
import base64

VOBIZ_SAMPLE_RATE = 8000
PIPELINE_SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2  # 16-bit


class AudioBridge:
    """One instance per call — holds the ratecv state for each direction
    so up/down-sampling doesn't introduce a discontinuity at every chunk
    boundary."""

    def __init__(self) -> None:
        self._in_state = None   # provider (8k) -> pipeline (16k)
        self._out_state = None  # pipeline (16k) -> provider (8k)

    def to_pcm16(self, payload_b64: str) -> bytes:
        """Base64 mu-law @ 8kHz (from the provider) -> raw PCM16 @ 16kHz
        (to Conversation Service)."""
        ulaw = base64.b64decode(payload_b64)
        pcm_8k = audioop.ulaw2lin(ulaw, _SAMPLE_WIDTH)
        pcm_16k, self._in_state = audioop.ratecv(
            pcm_8k, _SAMPLE_WIDTH, 1, VOBIZ_SAMPLE_RATE, PIPELINE_SAMPLE_RATE, self._in_state,
        )
        return pcm_16k

    def from_pcm16(self, pcm_16k: bytes) -> bytes:
        """Raw PCM16 @ 16kHz (from Conversation Service TTS) -> raw
        mu-law @ 8kHz bytes (not base64) — the pacer (bridge.py) frames
        and encodes these itself so it can pace delivery to real time
        instead of handing the provider the whole utterance at once."""
        pcm_8k, self._out_state = audioop.ratecv(
            pcm_16k, _SAMPLE_WIDTH, 1, PIPELINE_SAMPLE_RATE, VOBIZ_SAMPLE_RATE, self._out_state,
        )
        return audioop.lin2ulaw(pcm_8k, _SAMPLE_WIDTH)
