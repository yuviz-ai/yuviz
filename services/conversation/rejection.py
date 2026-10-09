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


class RejectionHandler:
    out_responses: "asyncio.Queue[HandlerResponse] | None" = None

    def __init__(self, status: RoutingStatus, tts: ITTS, sample_rate: int) -> None:
        self._message = REJECTION_MESSAGES[status]
        self._tts = tts
        self._sample_rate = sample_rate

    async def _speak(self) -> list[bytes]:
        try:
            return [c async for c in self._tts.synthesize_stream(self._message, self._sample_rate)]
        except Exception:
            log.exception("RejectionHandler: TTS failed; ending call silently")
            return []

    async def greeting(self, session_id: str) -> list[bytes]:
        return await self._speak()

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        return HandlerResponse()

    async def on_speech_ended(
        self, session_id: str, audio: bytes, duration_ms: int, energy_db: float,
    ) -> AsyncIterator[HandlerResponse]:
        # Caller spoke over the message (which cancels the pending hangup): say it again.
        # stt_text is a placeholder the gateway FSM needs to leave Recognizing.
        yield HandlerResponse(
            stt_text="(call rejected)", stt_confidence=1.0,
            tts_payloads=await self._speak(), end_call=True,
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
