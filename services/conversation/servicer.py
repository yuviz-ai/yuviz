"""
ConversationServicer — gRPC servicer bridging each bidi stream to ConversationSession.

Wire protocol per stream:
  1. Read SessionOpenRequest (first GatewayMessage — must arrive first)
  2. Yield ServiceReady
  3. Loop: read AudioChunk / SpeechEnded / CancelGeneration / PlaybackFinished
  4. Stream closes (StopAsyncIteration) → session.close()

An internal asyncio.Queue decouples gRPC message reading from processing.
This allows cancel_generation messages to be processed between pipeline
stages even when the servicer is inside a speech_ended() async-for loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, AsyncIterator, Callable

import grpc
import grpc.aio

from .event_bus import EventBus, TransferRequested
from .session import ConversationSession, IConversationHandler, SessionContext

from .generated.voiceai.v1 import conversation_pb2 as pb
from .generated.voiceai.v1 import conversation_pb2_grpc as pb_grpc

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0"


def _consume_pending_transfer(tr, interrupted: bool, sid: str):
    """Resolve a held TransferRequest at playback completion (both the
    outer-loop and in-pipeline playback_finished paths): returns the
    ServiceMessage to send, or None when it must not go out — barge-in
    dropped it (the LLM re-emits the directive if the caller still wants a
    human), or the destination is empty (defense in depth; pipeline.py's
    session-setup validation normally prevents that config from ever
    producing a directive).

    The barge-in-drops-it reasoning only holds for tr.trigger=="llm_directive":
    that's the caller's own request, so if they still want a human they'll
    just ask again. An "escalation_threshold" transfer is the SYSTEM's own
    decision (caller frustration, or a fabricated booking claim the caller
    has no way to know was false) — nothing prompts it to recur just
    because the caller says something unrelated in between. Confirmed live:
    a caller saying "Thank you" right after a fabricated
    "booked!" claim silently cancelled the safety-net transfer meant to
    catch exactly that — the correction was lost with no way to retrigger
    it. An escalation transfer must always go out once accepted."""
    if interrupted and tr.trigger != "escalation_threshold":
        log.info(
            "Converse: pending TransferRequest dropped "
            "(barge-in during acknowledgment) transfer_id=%s session=%s",
            tr.transfer_id, sid,
        )
        return None
    if not tr.destination:
        log.error(
            "Converse: refusing to send TransferRequest with empty destination "
            "transfer_id=%s session=%s — check the agent's transfer_destination "
            "config (Escalation tab)", tr.transfer_id, sid,
        )
        return None
    log.info(
        "Converse: sending TransferRequest type=%s destination=%s transfer_id=%s session=%s",
        tr.transfer_type.value, tr.destination, tr.transfer_id, sid,
    )
    return pb.ServiceMessage(
        transfer_request=pb.TransferRequest(
            session_id=sid,
            transfer_type=tr.transfer_type.value,
            destination=tr.destination,
            reason=tr.reason,
            transfer_id=tr.transfer_id,
            caller_id=tr.caller_id,
            waiting_experience=tr.waiting_experience,
        )
    )


class ConversationServicer(pb_grpc.ConversationServiceServicer):
    """
    Stateless servicer: creates a fresh ConversationSession per Converse() call.

    ``handler_factory`` receives the per-call SessionContext and returns a fresh
    IConversationHandler configured for that agent/tenant. Async because
    resolving that config may hit the Config Service (Postgres/Redis) — see
    services/conversation/agent_resolver.py.
    Example: ``async def factory(ctx): return PipelineConversationHandler(...)``
    """

    def __init__(
        self, handler_factory: Callable[[SessionContext], Awaitable[IConversationHandler]],
    ) -> None:
        self._handler_factory = handler_factory

    async def Converse(
        self,
        request_iterator: AsyncIterator[pb.GatewayMessage],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb.ServiceMessage]:

        # ── 1. Handshake ───────────────────────────────────────────────────────
        try:
            first = await request_iterator.__anext__()
        except StopAsyncIteration:
            log.warning("Stream closed before SessionOpenRequest")
            return

        if not first.HasField("session_open"):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "First message must be session_open",
            )
            return

        open_req = first.session_open
        client_ver = open_req.protocol_version

        if not client_ver.startswith("1."):
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                f"Unsupported protocol_version={client_ver!r}; server speaks 1.x",
            )
            return

        sid = open_req.session_id
        log.info("Converse: session_open session=%s tenant=%s",
                 sid, open_req.tenant_id)

        ctx = SessionContext(
            session_id=sid,
            tenant_id=open_req.tenant_id,
            trace_id=open_req.trace_id,
            call_id=open_req.call_id,
            caller_did=open_req.caller_did,
            called_did=open_req.called_did,
            direction=open_req.direction,
            script_id=open_req.script_id,
        )

        bus     = EventBus()
        handler = await self._handler_factory(ctx)  # fresh instance per stream, agent-aware
        session = ConversationSession(ctx=ctx, bus=bus, handler=handler)
        session.session_ready()
        await bus.start()

        # ── 2. ServiceReady ────────────────────────────────────────────────────
        yield pb.ServiceMessage(
            service_ready=pb.ServiceReady(
                session_id=sid,
                protocol_version=PROTOCOL_VERSION,
            )
        )

        tts_seq = 0

        # ── 3. Start reader before greeting ────────────────────────────────────
        # The reader task must be running BEFORE greeting synthesis so the gateway
        # can keep writing AudioChunk messages while TTS is being prepared.
        # Without this, the gRPC send buffer fills up during ~5 s of TTS synthesis
        # and the gateway write fails.  The queue is unbounded so audio arriving
        # during greeting is buffered and processed after greeting finishes.
        msg_q: asyncio.Queue[pb.GatewayMessage | None] = asyncio.Queue()

        async def _reader() -> None:
            try:
                async for msg in request_iterator:
                    await msg_q.put(msg)
            except Exception:
                log.debug("Converse: reader task exited session=%s", sid)
            finally:
                await msg_q.put(None)

        reader_task = asyncio.create_task(_reader())

        # Transfer waiting for its turn's spoken acknowledgment to finish
        # playing. The gateway executes uuid_transfer the moment it receives
        # TransferRequest (no playback-drain hold like EndCall's), so sending
        # it before playback_finished would cut off "connecting you now"
        # mid-sentence. Dropped on barge-in — the LLM re-emits the directive
        # if the caller still wants a human.
        pending_transfer = None
        # Set when TransferInitiated arrives; used to log duration_ms on the
        # eventual TransferCompleted/TransferFailed. Observability only.
        transfer_started_at: float | None = None

        # ── 2b. Greeting ───────────────────────────────────────────────────────
        # Synthesize the agent's opening line and stream it to the caller.
        # Audio arriving from the gateway during synthesis accumulates in msg_q.
        async for response in session.greet():
            if response.tts_payloads:
                yield pb.ServiceMessage(
                    tts_started=pb.TtsStarted(session_id=sid)
                )
                for i, payload in enumerate(response.tts_payloads):
                    tts_seq += 1
                    yield pb.ServiceMessage(
                        tts_chunk=pb.TtsChunk(
                            session_id=sid,
                            sequence_num=tts_seq,
                            codec=pb.AUDIO_CODEC_PCM_S16LE,
                            sample_rate=16000,
                            payload=payload,
                            is_final=(i == len(response.tts_payloads) - 1),
                        )
                    )

        async def _emit_response(response) -> AsyncIterator[pb.ServiceMessage]:
            """Out-of-band egress for a HandlerResponse with no inbound
            message driving it (a call-flow menu timeout — see
            callflow/handler.py's out_responses queue). Reproduces the
            speech_ended branch's exact order — TtsStarted -> TtsChunks ->
            terminal empty is_final=True chunk -> EndCall / held
            TransferRequest — but unconditionally, not gated on whether this
            turn had any tts_payloads: a hangup node with no prompt must
            still emit the terminal empty chunk before EndCall, or the
            gateway (which only consumes a pending end-call once that turn's
            TTS finishes playing) never gets the signal and the call hangs
            open until its own timeout."""
            nonlocal tts_seq, pending_transfer

            yield pb.ServiceMessage(tts_started=pb.TtsStarted(session_id=sid))
            for payload_bytes in response.tts_payloads:
                tts_seq += 1
                yield pb.ServiceMessage(
                    tts_chunk=pb.TtsChunk(
                        session_id=sid,
                        sequence_num=tts_seq,
                        codec=pb.AUDIO_CODEC_PCM_S16LE,
                        sample_rate=16000,
                        payload=payload_bytes,
                        is_final=False,
                    )
                )
            tts_seq += 1
            yield pb.ServiceMessage(
                tts_chunk=pb.TtsChunk(
                    session_id=sid,
                    sequence_num=tts_seq,
                    codec=pb.AUDIO_CODEC_PCM_S16LE,
                    sample_rate=16000,
                    payload=b"",
                    is_final=True,
                )
            )

            if response.end_call:
                yield pb.ServiceMessage(
                    end_call=pb.EndCall(
                        session_id=sid,
                        reason="agent_ended_call",
                        grace_period_ms=response.end_call_grace_period_ms,
                    )
                )

            if response.transfer_request:
                tr = response.transfer_request
                # Out-of-band responses always sent TTS above (unconditionally,
                # unlike the in-turn branch), so a transfer here always waits
                # for that playback to finish — same reasoning as the
                # speech_ended branch's pending_transfer.
                pending_transfer = tr
                log.info(
                    "Converse: TransferRequest held until playback finishes "
                    "(out-of-band) session=%s", sid,
                )

        msg_fut: asyncio.Future | None = None
        out_fut: asyncio.Future | None = None

        try:
            while True:
                if session.out_responses is not None:
                    if msg_fut is None:
                        msg_fut = asyncio.ensure_future(msg_q.get())
                    if out_fut is None:
                        out_fut = asyncio.ensure_future(session.out_responses.get())
                    done, _ = await asyncio.wait(
                        {msg_fut, out_fut}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if out_fut in done:
                        out_response = out_fut.result()
                        out_fut = None
                        async for out_msg in _emit_response(out_response):
                            yield out_msg
                        continue  # msg_fut (if any) stays pending for next iteration
                    msg = msg_fut.result()
                    msg_fut = None
                else:
                    msg = await msg_q.get()

                if msg is None:
                    break  # gRPC stream closed

                payload_case = msg.WhichOneof("payload")

                if payload_case == "audio_chunk":
                    chunk    = msg.audio_chunk
                    response = await session.push_audio(
                        chunk.payload, trace_id=chunk.trace_id
                    )

                    if response.stt_text:
                        yield pb.ServiceMessage(
                            stt_result=pb.SttResult(
                                session_id=sid,
                                trace_id=chunk.trace_id,
                                text=response.stt_text,
                                confidence=response.stt_confidence,
                                is_final=True,
                            )
                        )

                    if response.tts_payloads:
                        yield pb.ServiceMessage(
                            tts_started=pb.TtsStarted(
                                session_id=sid,
                                trace_id=chunk.trace_id,
                            )
                        )
                        for i, payload in enumerate(response.tts_payloads):
                            tts_seq += 1
                            yield pb.ServiceMessage(
                                tts_chunk=pb.TtsChunk(
                                    session_id=sid,
                                    trace_id=chunk.trace_id,
                                    sequence_num=tts_seq,
                                    codec=pb.AUDIO_CODEC_PCM_S16LE,
                                    sample_rate=16000,
                                    payload=payload,
                                    is_final=(i == len(response.tts_payloads) - 1),
                                )
                            )

                elif payload_case == "speech_ended":
                    se = msg.speech_ended
                    # Stream SttResult/TTS chunks as the pipeline produces them —
                    # buffering the whole turn would trip the gateway's SttTimeout.
                    tts_started_sent      = False
                    cancelled             = False
                    stt_sent              = False
                    end_call_requested    = False
                    end_call_grace_period = 0
                    turn_transfer         = None

                    async for response in session.speech_ended(
                        se.duration_ms, se.energy_db
                    ):
                        # Drain BEFORE sending so a cancel_generation that arrived
                        # during synthesis stops this turn's audio from being sent.
                        while not msg_q.empty():
                            pending = msg_q.get_nowait()
                            if pending is None:
                                # Stream ended during pipeline — stop generating.
                                await session.cancel()
                                cancelled = True
                                await msg_q.put(None)  # re-enqueue so outer loop exits
                                break
                            pcase = pending.WhichOneof("payload")
                            if pcase == "cancel_generation":
                                await session.cancel()
                                cancelled = True
                                # Ack now so C++ leaves BargeIn without waiting for
                                # the 500 ms barge_in_window; don't break — let the
                                # generator exhaust to release pipeline resources.
                                yield pb.ServiceMessage(
                                    cancel_ack=pb.CancelAck(
                                        session_id=sid,
                                        trace_id=pending.cancel_generation.trace_id,
                                    )
                                )
                            elif pcase == "playback_finished":
                                pf = pending.playback_finished
                                log.info(
                                    "Converse: playback_finished interrupted=%s (in-pipeline) session=%s",
                                    pf.interrupted, sid,
                                )
                                session.on_playback_finished(pf.interrupted)
                                if pending_transfer is not None:
                                    tr, pending_transfer = pending_transfer, None
                                    out = _consume_pending_transfer(tr, pf.interrupted, sid)
                                    if out is not None:
                                        yield out
                                    elif pf.interrupted:
                                        session.on_transfer_cancelled(tr.transfer_id)
                            elif pcase == "audio_chunk":
                                if cancelled:
                                    # Post-cancel frames are the caller's barge-in
                                    # utterance flushed by the gateway — keep them.
                                    await session.push_audio(
                                        pending.audio_chunk.payload,
                                        trace_id=pending.audio_chunk.trace_id,
                                    )
                                else:
                                    # Trailing frames of the utterance already being
                                    # processed — safe to discard.
                                    log.debug(
                                        "Discarding trailing audio_chunk in pipeline session=%s", sid,
                                    )
                            elif pcase == "speech_ended":
                                # New utterance while this turn's pipeline still runs —
                                # re-enqueue for the outer loop.
                                await msg_q.put(pending)
                                break
                            elif pcase in (
                                "transfer_initiated", "transfer_completed", "transfer_failed",
                            ):
                                # Deferred to the outer loop rather than handled here, for
                                # two reasons: transfer_completed/transfer_initiated don't
                                # need special in-pipeline timing, and transfer_failed
                                # (Phase 5C) streams its own TTS apology — interleaving that
                                # with this turn's still-in-flight TTS would produce
                                # overlapping/out-of-order audio for the gateway. Same
                                # "re-enqueue for the outer loop" pattern already used for
                                # speech_ended above.
                                await msg_q.put(pending)
                                break
                            else:
                                log.warning(
                                    "Unexpected %s during speech pipeline session=%s",
                                    pcase, sid,
                                )

                        if not cancelled:
                            if response.end_call:
                                end_call_requested    = True
                                end_call_grace_period = response.end_call_grace_period_ms
                            # Recorded now, sent to the gateway at end of turn
                            # (or held until playback_finished — see below).
                            if response.transfer_request:
                                tr = response.transfer_request
                                log.info(
                                    "Converse: transfer requested trigger=%s type=%s "
                                    "destination=%s session=%s",
                                    tr.trigger, tr.transfer_type.value, tr.destination, sid,
                                )
                                bus.publish(TransferRequested(
                                    session_id=tr.session_id,
                                    tenant_id=tr.tenant_id,
                                    call_id=tr.call_id,
                                    transfer_type=tr.transfer_type.value,
                                    destination=tr.destination,
                                    reason=tr.reason,
                                    trigger=tr.trigger,
                                ))
                                turn_transfer = tr
                            # Send STT result immediately — advances C++ FSM
                            # Recognizing→Thinking before SttTimeout (8 s) fires.
                            if response.stt_text:
                                stt_sent = True
                                yield pb.ServiceMessage(
                                    stt_result=pb.SttResult(
                                        session_id=sid,
                                        text=response.stt_text,
                                        confidence=response.stt_confidence,
                                        is_final=True,
                                    )
                                )
                            # Stream TTS chunks as each sentence is synthesized —
                            # advances C++ FSM Thinking→Synthesizing→Speaking so
                            # barge-in detection works during playback.
                            if response.tts_payloads:
                                if not tts_started_sent:
                                    yield pb.ServiceMessage(
                                        tts_started=pb.TtsStarted(session_id=sid)
                                    )
                                    tts_started_sent = True
                                for payload_bytes in response.tts_payloads:
                                    tts_seq += 1
                                    yield pb.ServiceMessage(
                                        tts_chunk=pb.TtsChunk(
                                            session_id=sid,
                                            sequence_num=tts_seq,
                                            codec=pb.AUDIO_CODEC_PCM_S16LE,
                                            sample_rate=16000,
                                            payload=payload_bytes,
                                            is_final=False,
                                        )
                                    )
                            # The assistant's complete spoken-turn text — sent
                            # alongside, never instead of, the tts_chunk audio
                            # already streamed above. Today's only consumer is
                            # the browser test-call panel (services/webcall),
                            # which has no other way to show what the agent said.
                            if response.response_text:
                                yield pb.ServiceMessage(
                                    assistant_response=pb.AssistantResponse(
                                        session_id=sid, text=response.response_text,
                                    )
                                )

                    # Empty transcript (noise / filtered hallucination): tell the
                    # gateway so its FSM returns to Listening instead of waiting
                    # out the 8 s SttTimeout in silence.
                    if not cancelled and not stt_sent:
                        yield pb.ServiceMessage(
                            stt_result=pb.SttResult(
                                session_id=sid, text="", confidence=0.0, is_final=True,
                            )
                        )

                    # End-of-response marker: empty is_final chunk; the gateway
                    # holds playback_finished until the final frame drains.
                    if tts_started_sent and not cancelled:
                        tts_seq += 1
                        yield pb.ServiceMessage(
                            tts_chunk=pb.TtsChunk(
                                session_id=sid,
                                sequence_num=tts_seq,
                                codec=pb.AUDIO_CODEC_PCM_S16LE,
                                sample_rate=16000,
                                payload=b"",
                                is_final=True,
                            )
                        )

                    # Agent decided this turn ends the call.  Sent after the
                    # final TtsChunk so the gateway holds this until the
                    # goodbye audio has actually drained (see CallSession's
                    # on_playback_finished handler) — a caller barge-in
                    # during that playback overrides this on the gateway side.
                    #
                    # Gated on tts_started_sent: the gateway only consumes its
                    # pending-end-call flag when that turn's TTS finishes
                    # playing (Speaking→WaitingForHangup).  If this turn had
                    # no TTS at all (e.g. the LLM emitted only the [[END_CALL]]
                    # marker with no spoken text before it — a model not
                    # following the "after your spoken words" instruction),
                    # the gateway's FSM never enters Speaking for this turn,
                    # so the flag would never be consumed here and would
                    # instead leak into a later, unrelated turn's playback
                    # completion, ending the call at the wrong moment.
                    if end_call_requested and not cancelled and tts_started_sent:
                        log.info(
                            "Converse: sending EndCall grace_period_ms=%d session=%s",
                            end_call_grace_period, sid,
                        )
                        yield pb.ServiceMessage(
                            end_call=pb.EndCall(
                                session_id=sid,
                                reason="agent_ended_call",
                                grace_period_ms=end_call_grace_period,
                            )
                        )
                    elif end_call_requested and not cancelled and not tts_started_sent:
                        log.warning(
                            "Converse: end-call marker detected but turn produced no "
                            "TTS audio — not sending EndCall (would leak into a later "
                            "turn on the gateway side) session=%s", sid,
                        )

                    # Transfer directive this turn: if the agent spoke an
                    # acknowledgment, hold the TransferRequest until that audio
                    # finishes playing (the gateway acts on it immediately —
                    # see pending_transfer's init comment); if the turn had no
                    # TTS there is no playback_finished coming, so send now.
                    if turn_transfer is not None and not cancelled:
                        if tts_started_sent:
                            pending_transfer = turn_transfer
                            log.info(
                                "Converse: TransferRequest held until playback "
                                "finishes session=%s", sid,
                            )
                        else:
                            log.info(
                                "Converse: TransferRequest dispatching immediately "
                                "(no TTS this turn) transfer_id=%s session=%s",
                                turn_transfer.transfer_id, sid,
                            )
                            out = _consume_pending_transfer(turn_transfer, False, sid)
                            if out is not None:
                                yield out

                elif payload_case == "dtmf":
                    # Never log the digit value (see the SDK's "digit values
                    # are never logged" rule) — arm name and session_id only.
                    log.info("Converse: dtmf session=%s", sid)
                    await session.push_dtmf(msg.dtmf.digit)

                elif payload_case == "cancel_generation":
                    await session.cancel()
                    yield pb.ServiceMessage(
                        cancel_ack=pb.CancelAck(
                            session_id=sid,
                            trace_id=msg.cancel_generation.trace_id,
                        )
                    )

                elif payload_case == "playback_finished":
                    pf = msg.playback_finished
                    log.info("Converse: playback_finished interrupted=%s session=%s",
                             pf.interrupted, sid)
                    session.on_playback_finished(pf.interrupted)
                    if pending_transfer is not None:
                        tr, pending_transfer = pending_transfer, None
                        out = _consume_pending_transfer(tr, pf.interrupted, sid)
                        if out is not None:
                            yield out
                        elif pf.interrupted:
                            session.on_transfer_cancelled(tr.transfer_id)

                # Phase 5B of AI-to-human transfer: purely reactive — drives
                # ConversationSession's own FSM/EventBus (see session.py).
                # No LLM/prompt change, no workflow change results from this one.
                elif payload_case == "transfer_initiated":
                    ti = msg.transfer_initiated
                    transfer_started_at = time.monotonic()
                    log.info("Converse: transfer_initiated type=%s destination=%s reason=%s "
                             "transfer_id=%s session=%s",
                             ti.transfer_type, ti.destination, ti.reason, ti.transfer_id, sid)
                    session.on_transfer_initiated(ti.transfer_type, ti.destination, ti.reason,
                                                  ti.transfer_id)

                # Phase 5D of AI-to-human transfer: runs SessionFinalizer's
                # post-call cleanup (summary generation, transcript, final
                # metrics — see session_finalizer.py) before telling the
                # gateway it may tear its own side down. The gateway is
                # actually waiting on ConversationFinalized (see CallFSM's
                # Finalizing state) — this is not just observability.
                elif payload_case == "transfer_completed":
                    tc = msg.transfer_completed
                    duration_ms = (
                        int((time.monotonic() - transfer_started_at) * 1000)
                        if transfer_started_at is not None else -1
                    )
                    log.info("Converse: transfer_completed destination=%s transfer_id=%s "
                             "duration_ms=%d session=%s",
                             tc.destination, tc.transfer_id, duration_ms, sid)
                    result = await session.on_transfer_completed(tc.destination, tc.transfer_id)
                    log.info(
                        "Converse: conversation_finalized session=%s summary_generated=%s "
                        "transcript_written=%s status=%s",
                        sid, result.summary_generated, result.transcript_written,
                        result.status.value,
                    )
                    yield pb.ServiceMessage(
                        conversation_finalized=pb.ConversationFinalized(
                            session_id=sid,
                            reason=pb.TRANSFER_SUCCESS,
                            summary_generated=result.summary_generated,
                            transcript_written=result.transcript_written,
                        )
                    )

                # Phase 5C of AI-to-human transfer: unlike the two above,
                # this one owns workflow/TTS/memory/metrics — it streams a
                # generated apology back to the gateway (same tts_started/
                # tts_chunk/is_final sequencing as the speech_ended branch
                # above) instead of just reacting silently.
                elif payload_case == "transfer_failed":
                    tf = msg.transfer_failed
                    duration_ms = (
                        int((time.monotonic() - transfer_started_at) * 1000)
                        if transfer_started_at is not None else -1
                    )
                    log.info("Converse: transfer_failed destination=%s reason=%s transfer_id=%s "
                             "duration_ms=%d session=%s",
                             tf.destination, tf.reason, tf.transfer_id, duration_ms, sid)

                    tf_tts_started_sent = False
                    async for response in session.on_transfer_failed(tf.destination, tf.reason,
                                                                     tf.transfer_id):
                        if response.tts_payloads:
                            if not tf_tts_started_sent:
                                yield pb.ServiceMessage(tts_started=pb.TtsStarted(session_id=sid))
                                tf_tts_started_sent = True
                            for payload_bytes in response.tts_payloads:
                                tts_seq += 1
                                yield pb.ServiceMessage(
                                    tts_chunk=pb.TtsChunk(
                                        session_id=sid,
                                        sequence_num=tts_seq,
                                        codec=pb.AUDIO_CODEC_PCM_S16LE,
                                        sample_rate=16000,
                                        payload=payload_bytes,
                                        is_final=False,
                                    )
                                )

                    if tf_tts_started_sent:
                        tts_seq += 1
                        yield pb.ServiceMessage(
                            tts_chunk=pb.TtsChunk(
                                session_id=sid,
                                sequence_num=tts_seq,
                                codec=pb.AUDIO_CODEC_PCM_S16LE,
                                sample_rate=16000,
                                payload=b"",
                                is_final=True,
                            )
                        )

                else:
                    log.warning("Unknown payload_case=%s session=%s",
                                payload_case, sid)

        except Exception as exc:
            log.exception("Converse error session=%s", sid)
            yield pb.ServiceMessage(
                error=pb.ServiceError(
                    session_id=sid,
                    code="INTERNAL",
                    message=str(exc),
                    fatal=True,
                )
            )

        # ── 4. Teardown ────────────────────────────────────────────────────────
        finally:
            for pending_fut in (msg_fut, out_fut):
                if pending_fut is not None and not pending_fut.done():
                    pending_fut.cancel()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

            await session.close("stream_ended")
            # Drain the bus before stopping so SessionEnded and other close-path
            # events are delivered to subscribers.
            try:
                await asyncio.wait_for(bus.drain(), timeout=0.5)
            except asyncio.TimeoutError:
                log.debug("Converse: bus drain timed out session=%s", sid)
            await bus.stop()
            log.info("Converse: stream closed session=%s", sid)
