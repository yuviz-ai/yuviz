"""EchoConversationHandler: loopback stub that echoes inbound audio as TTS, for
integration/load testing the gateway FSM."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .session import HandlerResponse
from .session_finalizer import FinalizationResult, FinalizationStatus


class EchoConversationHandler:
    """
    IConversationHandler that echoes audio back after simulating STT+LLM+TTS.

    pipeline_delay_ms: artificial delay applied once per on_audio() call.
    """

    # No out-of-band egress.
    out_responses: "asyncio.Queue[HandlerResponse] | None" = None

    def __init__(self, pipeline_delay_ms: float = 0.0) -> None:
        self._delay_s = pipeline_delay_ms / 1000.0

    async def greeting(self, session_id: str) -> list[bytes]:
        return []

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        if not payload:
            return HandlerResponse()

        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)

        return HandlerResponse(
            stt_text="echo",
            stt_confidence=1.0,
            tts_payloads=[payload],
        )

    async def on_speech_ended(
        self,
        session_id:  str,
        audio:       bytes,
        duration_ms: int,
        energy_db:   float,
    ) -> AsyncIterator[HandlerResponse]:
        # Echo mode responds immediately in on_audio(); nothing to do here.
        return
        yield  # make this an async generator

    async def on_cancel(self, session_id: str) -> None:
        pass

    async def on_dtmf(self, session_id: str, digit: str) -> None:
        # Echo mode has no IVR/flow to advance on a keypress.
        pass

    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None:
        pass

    async def on_transfer_failed(
        self, session_id: str, destination: str, reason: str,
    ) -> AsyncIterator[HandlerResponse]:
        return
        yield  # make this an async generator

    def on_transfer_cancelled(self, session_id: str) -> None:
        pass

    def start_finalization(self, session_id: str) -> None:
        # Echo mode has no LLM to speculatively summarize with.
        pass

    async def finalize_session(self, session_id: str, reason: str) -> FinalizationResult:
        return FinalizationResult(
            summary="", summary_generated=False, transcript_written=False,
            status=FinalizationStatus.COMPLETED,
        )

    def record_live_stage(self, session_id: str, stage: str) -> None:
        # Echo mode has no transcripts to persist live_stage against.
        pass
