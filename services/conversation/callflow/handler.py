"""IConversationHandler that turns CallFlowRunner actions into audio, timers and
egress, then delegates to a pipeline handler at an `agent` node.

Only the driver task mutates the runner; DTMF and timers just enqueue events.
Timeouts carry a per-arm generation (a node can be replayed), so stale ones are dropped.
All flow audio leaves via `out_responses`."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Awaitable, Callable

from ..directives import TransferRequest, TransferType
from ..session import HandlerResponse
from ..session_finalizer import FinalizationResult, FinalizationStatus
from .runner import Action, CallFlowRunner, Dial, Handoff, Hangup, Listen, Speak, SetVoice, Store

log = logging.getLogger(__name__)

# ("digit", digit) | ("timeout", generation)
_FlowEvent = tuple[str, Any]

# ITTS output is S16LE mono.
_BYTES_PER_SAMPLE = 2


async def _empty_async_gen() -> AsyncGenerator[HandlerResponse, None]:
    return
    yield  # pragma: no cover — makes this an async generator


class CallFlowConversationHandler:
    """One caller's call-flow session. Handler methods are inert until handoff,
    then forward to the delegate."""

    def __init__(
        self,
        runner: CallFlowRunner,
        *,
        tts: Any,
        sample_rate: int,
        session_id: str,
        tenant_id: str,
        call_id: str,
        handoff: Callable[[str, dict[str, Any]], Awaitable[Any | None]],
        voice_for: Callable[[str], Awaitable[Any | None]],
        agent_slugs: dict[str, str],
    ) -> None:
        self._runner = runner
        self._default_tts = tts
        self._active_tts = tts
        self._sample_rate = sample_rate
        self._session_id = session_id
        self._tenant_id = tenant_id
        self._call_id = call_id
        self._handoff = handoff
        self._voice_for = voice_for
        self._agent_slugs = agent_slugs

        self._delegate: Any | None = None
        self._events: asyncio.Queue[_FlowEvent] = asyncio.Queue()
        self._timers: set[asyncio.Task] = set()
        # Bumped on every Listen, including same-node replays; used to drop stale timeouts.
        self._listen_generation = 0
        # Set on Hangup/Dial so queued events don't re-drive a finished runner.
        self._ended = False

        self.out_responses: asyncio.Queue[HandlerResponse] = asyncio.Queue()
        self._driver_task = asyncio.ensure_future(self._drive())

    # ── The single mutator ──────────────────────────────────────────────

    async def _drive(self) -> None:
        try:
            await self._run_actions(self._runner.open())
            while not self._ended and self._delegate is None:
                kind, value = await self._events.get()
                if kind == "timeout":
                    if value != self._listen_generation:
                        continue  # stale — a later Listen has since been armed
                    actions = self._runner.on_timeout()
                else:
                    actions = self._runner.on_digit(value)
                if not actions:
                    continue  # ignored input, or a replay not yet due
                await self._run_actions(actions)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("callflow: driver task failed session=%s", self._session_id)
            self._ended = True
            await self.out_responses.put(HandlerResponse(end_call=True))

    async def _run_actions(self, actions: list[Action]) -> None:
        """Each action list ends in exactly one Listen/Hangup/Dial/Handoff."""
        self._cancel_timers()
        pcm: list[bytes] = []
        for action in actions:
            if isinstance(action, SetVoice):
                await self._set_voice(action.tts_config_id)
            elif isinstance(action, Speak):
                pcm.append(await self._active_tts.synthesize(action.text, self._sample_rate))
            elif isinstance(action, Store):
                pass  # already applied inside the runner — see _submit_collect()
            elif isinstance(action, Listen):
                self._arm_timer(action, pcm)
                if pcm:
                    await self.out_responses.put(HandlerResponse(tts_payloads=pcm))
                return
            elif isinstance(action, Hangup):
                self._ended = True
                await self.out_responses.put(HandlerResponse(tts_payloads=pcm, end_call=True))
                return
            elif isinstance(action, Dial):
                self._ended = True
                await self.out_responses.put(HandlerResponse(
                    tts_payloads=pcm,
                    transfer_request=TransferRequest(
                        session_id=self._session_id, tenant_id=self._tenant_id,
                        call_id=self._call_id, transfer_type=TransferType.COLD,
                        destination=action.destination, reason="call_flow_dial",
                    ),
                ))
                return
            elif isinstance(action, Handoff):
                await self._handoff_to(action.agent_id, pcm)
                return

    async def _set_voice(self, tts_config_id: str | None) -> None:
        if tts_config_id is None:
            self._active_tts = self._default_tts
            return
        voice = await self._voice_for(tts_config_id)
        self._active_tts = voice if voice is not None else self._default_tts

    def _arm_timer(self, listen: Listen, pcm: list[bytes]) -> None:
        self._listen_generation += 1
        generation = self._listen_generation
        audio_s = sum(len(p) for p in pcm) / (self._sample_rate * _BYTES_PER_SAMPLE)
        delay = audio_s + listen.timeout_ms / 1000.0

        async def _fire() -> None:
            await asyncio.sleep(delay)
            self._events.put_nowait(("timeout", generation))

        task = asyncio.ensure_future(_fire())
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    def _cancel_timers(self) -> None:
        for task in self._timers:
            task.cancel()

    async def _handoff_to(self, agent_id: str, pcm: list[bytes]) -> None:
        agent_slug = self._agent_slugs.get(agent_id)
        if not agent_slug:
            log.error(
                "callflow: agent node names an agent not visible to this "
                "flow session=%s agent_id=%s — ending call",
                self._session_id, agent_id,
            )
            await self.out_responses.put(HandlerResponse(tts_payloads=pcm, end_call=True))
            return

        delegate = await self._handoff(agent_slug, self._runner.variables)
        if delegate is None:
            log.error(
                "callflow: handoff to agent=%s could not resolve — ending "
                "call session=%s", agent_slug, self._session_id,
            )
            await self.out_responses.put(HandlerResponse(tts_payloads=pcm, end_call=True))
            return

        self._delegate = delegate
        greeting = await delegate.greeting(self._session_id)
        payloads = pcm + list(greeting)
        if payloads:
            await self.out_responses.put(HandlerResponse(tts_payloads=payloads))

    # ── on_dtmf: the other enqueue-only producer ─────────────────────────

    async def on_dtmf(self, session_id: str, digit: str) -> None:
        if self._delegate is not None:
            await self._delegate.on_dtmf(session_id, digit)
            return
        self._events.put_nowait(("digit", digit))

    # ── Every other IConversationHandler method: delegate-or-inert ──────

    async def greeting(self, session_id: str) -> list[bytes]:
        if self._delegate is not None:
            return await self._delegate.greeting(session_id)
        return []

    async def on_audio(self, session_id: str, payload: bytes) -> HandlerResponse:
        if self._delegate is not None:
            return await self._delegate.on_audio(session_id, payload)
        return HandlerResponse()

    def on_speech_ended(
        self, session_id: str, audio: bytes, duration_ms: int, energy_db: float,
    ) -> AsyncGenerator[HandlerResponse, None]:
        if self._delegate is not None:
            return self._delegate.on_speech_ended(session_id, audio, duration_ms, energy_db)
        return _empty_async_gen()

    async def on_cancel(self, session_id: str) -> None:
        if self._delegate is not None:
            await self._delegate.on_cancel(session_id)

    async def on_session_end(
        self, session_id: str, reason: str, final_state: str | None = None,
    ) -> None:
        self._cancel_timers()
        pending = list(self._timers)
        if not self._driver_task.done():
            self._driver_task.cancel()
            pending.append(self._driver_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._delegate is not None:
            await self._delegate.on_session_end(session_id, reason, final_state=final_state)

    def on_transfer_failed(
        self, session_id: str, destination: str, reason: str,
    ) -> AsyncGenerator[HandlerResponse, None]:
        if self._delegate is not None:
            return self._delegate.on_transfer_failed(session_id, destination, reason)
        return _empty_async_gen()

    def on_transfer_cancelled(self, session_id: str) -> None:
        if self._delegate is not None:
            self._delegate.on_transfer_cancelled(session_id)

    def start_finalization(self, session_id: str) -> None:
        if self._delegate is not None:
            self._delegate.start_finalization(session_id)

    async def finalize_session(self, session_id: str, reason: str) -> FinalizationResult:
        if self._delegate is not None:
            return await self._delegate.finalize_session(session_id, reason)
        return FinalizationResult(
            summary="", summary_generated=False, transcript_written=False,
            status=FinalizationStatus.COMPLETED,
        )

    def record_live_stage(self, session_id: str, stage: str) -> None:
        if self._delegate is not None:
            self._delegate.record_live_stage(session_id, stage)
