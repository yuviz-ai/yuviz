"""RejectionHandler: speaks a fixed message and ends the call, for calls the gateway
could not route. Loads no agent and writes no call record, so nothing is attributed
to a tenant that does not own the number."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from .providers.interfaces import ITTS
from .session import HandlerResponse, RoutingStatus
from .session_finalizer import FinalizationResult, FinalizationStatus

log = logging.getLogger(__name__)

REJECTION_MESSAGES = {
    RoutingStatus.UNKNOWN:     "The number you have dialled is not in service. Goodbye.",
    RoutingStatus.UNAVAILABLE: "We are unable to take your call right now. Please try again later.",
}

# Repeats after the first talk-over get only silence, so a noisy line can't loop the TTS.
_MAX_SPOKEN_REPEATS = 1
_SILENCE_MS = 300
_HANGUP_GRACE_MS = 100

# Synthesized once per (message, sample_rate); failures are not cached.
_audio_cache: dict[tuple[str, int], list[bytes]] = {}


def _silence(sample_rate: int) -> list[bytes]:
    # Never send an empty turn: EndCall is only sent, and only consumed, after real audio.
    return [b"\x00\x00" * (sample_rate * _SILENCE_MS // 1000)]


async def _synthesize(message: str, tts: ITTS, sample_rate: int) -> list[bytes]:
    key = (message, sample_rate)
    cached = _audio_cache.get(key)
    if cached is not None:
        return cached
    try:
        chunks = [c async for c in tts.synthesize_stream(message, sample_rate)]
    except Exception:
        log.exception("RejectionHandler: TTS failed; ending call with silence")
        return _silence(sample_rate)
    if not chunks:
        return _silence(sample_rate)
    _audio_cache[key] = chunks
    return chunks


async def prewarm(tts: ITTS, sample_rate: int) -> None:
    """Startup only, so the first rejected call doesn't wait on the shared TTS."""
    for message in REJECTION_MESSAGES.values():
        await _synthesize(message, tts, sample_rate)


class RejectionHandler:
    out_responses: "asyncio.Queue[HandlerResponse] | None" = None

    def __init__(self, status: RoutingStatus, tts: ITTS, sample_rate: int) -> None:
        self._message = REJECTION_MESSAGES[status]
        self._tts = tts
        self._sample_rate = sample_rate
        self._repeats = 0

    async def _speak(self) -> list[bytes]:
        return await _synthesize(self._message, self._tts, self._sample_rate)

    async def greeting(self, session_id: str) -> list[bytes]:
        return await self._speak()

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        return HandlerResponse()

    async def on_speech_ended(
        self, session_id: str, audio: bytes, duration_ms: int, energy_db: float,
    ) -> AsyncIterator[HandlerResponse]:
        # Caller spoke over the message (which cancels the pending hangup): say it again once,
        # then hang up after silence. stt_text is a placeholder the gateway FSM needs.
        self._repeats += 1
        if self._repeats <= _MAX_SPOKEN_REPEATS:
            yield HandlerResponse(
                stt_text="(call rejected)", stt_confidence=1.0,
                tts_payloads=await self._speak(), end_call=True,
            )
            return
        yield HandlerResponse(
            stt_text="(call rejected)", stt_confidence=1.0,
            tts_payloads=_silence(self._sample_rate), end_call=True,
            end_call_grace_period_ms=_HANGUP_GRACE_MS,
        )

    async def on_cancel(self, session_id: str) -> None:
        pass

    async def on_dtmf(self, session_id: str, digit: str) -> None:
        pass

    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None:
        pass

    async def on_transfer_failed(
        self, session_id: str, destination: str, reason: str,
    ) -> AsyncIterator[HandlerResponse]:
        return
        yield

    def on_transfer_cancelled(self, session_id: str) -> None:
        pass

    def start_finalization(self, session_id: str) -> None:
        pass

    async def finalize_session(self, session_id: str, reason: str) -> FinalizationResult:
        return FinalizationResult(
            summary="", summary_generated=False, transcript_written=False,
            status=FinalizationStatus.COMPLETED,
        )

    def record_live_stage(self, session_id: str, stage: str) -> None:
        pass
