"""
EchoConversationHandler — Phase 4 integration test stub.

Simulates the full STT→LLM→TTS pipeline by:
  1. Treating the raw inbound PCM payload as the "transcript" (echo).
  2. Returning stt_text="echo" and the original payload as a single TTS chunk.

This exercises the Gateway FSM's full state chain:
  Listening → Recognizing → Thinking → Synthesizing → Speaking → Listening

An optional pipeline_delay_ms introduces artificial latency to simulate real
provider round-trips during load and integration testing.
"""

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

    async def on_session_end(self, session_id: str, reason: str,
                             final_state: str | None = None) -> None:
        pass

    async def on_transfer_failed(
        self, session_id: str, destination: str, reason: str,
    ) -> AsyncIterator[HandlerResponse]:
        # Echo mode has no LLM/TTS to generate a real apology with —
        # nothing to do here beyond letting ConversationSession still drive
        # its own FSM/EventBus (see session.py).
        return
        yield  # make this an async generator

    def on_transfer_cancelled(self, session_id: str) -> None:
        # Echo mode never produces a transfer request, so it never has
        # duplicate-suppression bookkeeping to release.
        pass

    def start_finalization(self, session_id: str) -> None:
        # Echo mode has no LLM to speculatively summarize with.
        pass

    async def finalize_session(self, session_id: str, reason: str) -> FinalizationResult:
        # Echo mode has no LLM to summarize with and no transcripts to
        # persist — nothing to do here beyond letting ConversationSession
        # still drive its own FSM/EventBus (see session.py).
        return FinalizationResult(
            summary="", summary_generated=False, transcript_written=False,
            status=FinalizationStatus.COMPLETED,
        )

    def record_live_stage(self, session_id: str, stage: str) -> None:
        # Echo mode has no transcripts to persist live_stage against.
        pass
