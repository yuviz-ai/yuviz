"""A turn is persisted even when the caller hangs up mid-turn (#172)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer

from .test_pipeline import _make_handler, _make_llm, _make_stt, _make_tts, _silence


def _stalling_llm(spoken: str) -> MagicMock:
    async def _gen(messages):
        yield spoken
        await asyncio.Event().wait()   # the rest of the reply never arrives

    llm = MagicMock()
    llm.generate = _gen
    return llm


async def test_hangup_mid_reply_persists_the_turn_as_interrupted():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("book me in"), _stalling_llm("Sure thing."), _make_tts(), transcripts=transcripts,
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
        chunks = 0
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            if msg.HasField("tts_chunk"):
                chunks += 1
                # The first chunk is the filler; hang up once the reply sentence, the second, is sent.
                if chunks == 2:
                    first_audio.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_audio.wait(), 2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[:2] == ("s1", "book me in")
    assert args[3] == "Sure thing."
    assert args[4] is True


async def test_hangup_while_waiting_on_the_model_persists_the_caller_utterance():
    transcripts = MagicMock()

    async def never(messages):
        await asyncio.Event().wait()
        yield ""   # pragma: no cover

    llm = MagicMock()
    llm.generate = never
    handler = _make_handler(_make_stt("pay my order"), llm, _make_tts(), transcripts=transcripts)

    async def turn():
        async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
            pass

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[1] == "pay my order"
    assert args[4] is True


async def test_hangup_during_the_max_duration_goodbye_writes_one_interrupted_row():
    transcripts = MagicMock()
    tts = MagicMock()

    async def _stalled(text, sample_rate):
        await asyncio.Event().wait()   # the goodbye never finishes synthesizing
        yield b""   # pragma: no cover

    tts.synthesize_stream = _stalled
    handler = _make_handler(
        _make_stt("are you still there"), _stalling_llm("never used"), tts,
        max_call_duration_s=60, transcripts=transcripts,
    )
    handler._call_started_at -= 61

    async def turn():
        async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
            pass

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[1] == "are you still there"
    assert args[4] is True


async def test_hangup_parked_at_the_response_text_yield_writes_one_interrupted_row():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("book me in"), _make_llm(["Sure thing."]), _make_tts(), transcripts=transcripts,
    )

    turn = handler.on_speech_ended("s1", _silence(), 200, -20.0)
    async for response in turn:
        if response.on_sent:
            response.on_sent()   # the servicer's send
        if response.response_text:
            break
    await turn.aclose()   # the gateway hangs up while the generator waits at that yield

    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[1] == "book me in"
    assert args[3] == "Sure thing."
    assert args[4] is True


async def test_a_turn_that_finishes_normally_is_recorded_once_even_if_closed_afterwards():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("book me in"), _make_llm(["Sure thing."]), _make_tts(), transcripts=transcripts,
    )

    turn = handler.on_speech_ended("s1", _silence(), 200, -20.0)
    async for _ in turn:
        pass
    await turn.aclose()

    transcripts.record_turn.assert_called_once()
    assert transcripts.record_turn.call_args.args[4] is False


async def test_a_turn_abandoned_past_the_session_end_does_not_write_to_the_ended_call():
    """A turn the servicer abandoned at the close may finish after on_session_end: its row must be dropped."""
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("book me in"), _make_llm(["Sure thing."]), _make_tts(), transcripts=transcripts,
    )

    turn = handler.on_speech_ended("s1", _silence(), 200, -20.0)
    async for response in turn:
        if response.response_text:
            break
    await handler.on_session_end("s1", "stream_ended")
    await turn.aclose()   # the abandoned turn unwinds only after the session ended

    transcripts.record_turn.assert_not_called()
    assert "s1" not in handler._sessions
