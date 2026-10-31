"""A hangup while the servicer's concurrent gateway reader is racing a turn persists exactly one
interrupted row and leaves no turn task behind (#170 reader + #172 persistence)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer

from .test_pipeline import _make_handler, _make_stt, _make_tts, _silence


def _stalling_llm(stalled: asyncio.Event) -> MagicMock:
    async def _gen(messages):
        yield "Sure thing."
        stalled.set()
        await asyncio.Event().wait()   # the rest of the reply never arrives

    llm = MagicMock()
    llm.generate = _gen
    return llm


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


@pytest.mark.parametrize("hangup", ["generator_closed", "consumer_cancelled"])
async def test_hangup_mid_turn_writes_one_interrupted_row_and_leaks_no_task(hangup):
    transcripts = MagicMock()
    stalled = asyncio.Event()
    handler = _make_handler(
        _make_stt("book me in"), _stalling_llm(stalled), _make_tts(), transcripts=transcripts,
    )

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.Event().wait()   # the gateway holds the stream open until it hangs up

    first_audio = asyncio.Event()

    async def factory(ctx):
        return handler

    async def consume():
        converse = ConversationServicer(factory).Converse(stream(), AsyncMock())
        chunks = 0
        async for msg in converse:
            if msg.HasField("tts_chunk"):
                chunks += 1
                # The reply comes from the producer task a hop after the filler, so cancel once it is sent.
                if hangup == "generator_closed" or chunks == 2:
                    first_audio.set()
                # The first chunk is the filler; the reply sentence is the second, and only a sent one is stored.
                if hangup == "generator_closed" and chunks == 2:
                    await stalled.wait()
                    await asyncio.sleep(0.1)   # the reply so far reaches the pipeline's buffer
                    # Abandoned at a yield, as when the RPC is torn down between messages.
                    await converse.aclose()
                    return

    before = asyncio.all_tasks()
    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_audio.wait(), 2)
    if hangup == "generator_closed":
        await asyncio.wait_for(task, 2)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The row is written by the time the RPC returns, not later by garbage collection.
    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[:2] == ("s1", "book me in")
    assert args[3] == "Sure thing."
    assert args[4] is True
    await _settle()
    leaked = [t for t in asyncio.all_tasks() - before if not t.done() and t is not asyncio.current_task()]
    assert leaked == []


async def test_stream_close_ends_a_turn_stalled_before_its_first_token_promptly():
    """A half-close must cancel a turn waiting on the LLM, not wait for the LLM to time out; the turn is
    stored as interrupted and is not counted as a barge-in."""
    async def _never(messages):
        await asyncio.Event().wait()
        yield ""

    llm = MagicMock()
    llm.generate = _never
    transcripts = MagicMock()
    handler = _make_handler(_make_stt("book me in"), llm, _make_tts(), transcripts=transcripts)

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.sleep(0.2)   # the turn is now stalled on the LLM; the client half-closes

    async def factory(ctx):
        return handler

    async def consume():
        async for _ in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            pass

    await asyncio.wait_for(consume(), 1.0)

    transcripts.record_turn.assert_called_once()
    assert transcripts.record_turn.call_args.args[4] is True
    assert transcripts.record_turn.call_args.kwargs["count_barge_in"] is False
    assert "s1" not in handler._sessions   # session closed
