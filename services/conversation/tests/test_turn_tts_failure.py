"""A TTS failure must not leave the caller in silence or be stored as spoken text (#174)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer
from services.conversation.session import HandlerResponse

from ..pipeline import _END_CALL_MARKER
from .test_pipeline import (
    _FIRST_TURN_PHRASES,
    _make_handler,
    _make_llm,
    _make_slow_llm,
    _make_stt,
    _make_tts,
    _silence,
)


def _tts_failing_on(marker: str) -> MagicMock:
    tts = MagicMock()

    async def _stream(text, sample_rate):
        if marker in text:
            raise RuntimeError("tts down")
        yield b"\x00" * 320

    tts.synthesize_stream = _stream
    return tts


async def test_a_sentence_that_failed_tts_is_not_stored_as_spoken():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("when is it"),
        _make_llm(["Your booking QATTSFAIL is done.", " Anything else?"]),
        _tts_failing_on("QATTSFAIL"), transcripts=transcripts,
    )

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert transcripts.record_turn.call_args.args[3] == "Anything else?"
    assert handler._get_history("s1")[-1].content == "Anything else?"


async def test_a_reply_whose_tts_failed_entirely_is_not_added_to_history(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "60000")
    handler = _make_handler(
        _make_stt("when is it"), _make_llm(["Your booking QATTSFAIL is done."]),
        _tts_failing_on("QATTSFAIL"),
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0)]

    assert not any(r.tts_payloads or r.response_text for r in responses)
    assert [m.role for m in handler._get_history("s1")] == ["system"]


async def test_a_turn_with_no_audio_still_sends_the_terminal_chunk(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "60000")
    handler = _make_handler(
        _make_stt("when is it"), _make_llm(["Your booking QATTSFAIL is done."]),
        _tts_failing_on("QATTSFAIL"),
    )

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.Event().wait()

    async def factory(ctx):
        return handler

    kinds = []

    async def consume():
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            kinds.append(msg.WhichOneof("payload"))
            if msg.HasField("tts_chunk"):
                assert msg.tts_chunk.is_final and not msg.tts_chunk.payload
                return

    await asyncio.wait_for(consume(), 2)

    assert kinds[-3:] == ["stt_result", "tts_started", "tts_chunk"]


async def _kinds_after_turn(turn) -> list[str]:
    """Message kinds the servicer sends for one speech_ended turn, up to just after the final chunk."""
    handler = AsyncMock()
    handler.out_responses = None
    handler.on_speech_ended = turn

    async def factory(ctx):
        return handler

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.Event().wait()

    kinds = []
    done = asyncio.Event()

    async def consume():
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            kinds.append(msg.WhichOneof("payload"))
            if msg.HasField("tts_chunk") and msg.tts_chunk.is_final:
                done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 2)
        await asyncio.sleep(0.2)   # an EndCall sent after the final chunk would land here
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return kinds


async def test_an_end_call_turn_whose_tts_produced_no_audio_does_not_send_end_call():
    """The synthetic TtsStarted closes the turn but is not audio: EndCall would hang up silently, or
    stay latched on the gateway for the caller's next reply."""
    async def turn(session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(stt_text="bye")
        yield HandlerResponse(end_call=True)

    kinds = await _kinds_after_turn(turn)

    assert kinds[-3:] == ["stt_result", "tts_started", "tts_chunk"]
    assert "end_call" not in kinds


async def test_an_end_call_turn_that_played_only_a_filler_does_not_send_end_call():
    """A filler replays from cache while TTS is down, so hearing it does not mean the goodbye was heard."""
    async def turn(session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(stt_text="bye")
        yield HandlerResponse(tts_payloads=[b"\x00" * 320], filler=True)
        yield HandlerResponse(end_call=True)

    kinds = await _kinds_after_turn(turn)

    assert kinds[-4:] == ["stt_result", "tts_started", "tts_chunk", "tts_chunk"]
    assert "end_call" not in kinds


async def test_an_end_call_turn_whose_goodbye_played_after_a_filler_sends_end_call():
    async def turn(session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(stt_text="bye")
        yield HandlerResponse(tts_payloads=[b"\x00" * 320], filler=True)
        yield HandlerResponse(tts_payloads=[b"\x00" * 320])
        yield HandlerResponse(end_call=True)

    kinds = await _kinds_after_turn(turn)

    assert kinds[-1] == "end_call"


def _tts_speaking_only(phrases) -> MagicMock:
    """A provider that is down for everything except the cached filler phrases."""
    tts = MagicMock()

    async def _stream(text, sample_rate):
        if text not in phrases:
            raise RuntimeError("tts down")
        yield b"\x00" * 320

    tts.synthesize_stream = _stream
    return tts


async def test_a_filler_is_marked_and_a_turn_whose_reply_and_goodbye_failed_does_not_end_the_call(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "20")
    handler = _make_handler(
        _make_stt("goodbye"), _make_slow_llm(0.3, ["Bye now. ", _END_CALL_MARKER]),
        _tts_speaking_only(_FIRST_TURN_PHRASES),
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0)]

    audio = [r for r in responses if r.tts_payloads]
    assert audio and all(r.filler for r in audio)
    assert not any(r.end_call for r in responses)


async def test_a_spoken_reply_is_not_marked_as_a_filler_and_ends_the_call(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "20")
    handler = _make_handler(
        _make_stt("goodbye"), _make_slow_llm(0.3, ["Bye now. ", _END_CALL_MARKER]), _make_tts(),
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0)]

    assert any(r.tts_payloads and not r.filler for r in responses)
    assert any(r.end_call for r in responses)


def _tts_failing_on_exactly(sentence: str) -> MagicMock:
    tts = MagicMock()

    async def _stream(text, sample_rate):
        if text == sentence:
            raise RuntimeError("tts down")
        yield b"\x00" * 320

    tts.synthesize_stream = _stream
    return tts


def _reply_recorded(transcripts) -> str:
    return transcripts.record_turn.call_args.args[3]


async def test_a_failed_short_sentence_does_not_strip_the_same_characters_from_a_spoken_one():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("what is it"), _make_llm(["Your Account No. 4512 is ready.", " No."]),
        _tts_failing_on_exactly("No."), transcripts=transcripts,
    )

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert _reply_recorded(transcripts) == "Your Account No. 4512 is ready."
    assert handler._get_history("s1")[-1].content == "Your Account No. 4512 is ready."


async def test_only_the_first_occurrence_of_a_failed_sentence_is_removed():
    transcripts = MagicMock()
    calls = []

    tts = MagicMock()

    async def _stream(text, sample_rate):
        calls.append(text)
        if text == "Okay." and calls.count("Okay.") == 1:
            raise RuntimeError("tts down")
        yield b"\x00" * 320

    tts.synthesize_stream = _stream
    handler = _make_handler(
        _make_stt("ok"), _make_llm(["Okay.", " Done.", " Okay."]), tts, transcripts=transcripts,
    )

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert _reply_recorded(transcripts) == "Done. Okay."


async def test_a_failed_prewarm_filler_is_not_a_failed_reply_sentence():
    handler = _make_handler(_make_stt("hi"), _make_llm(["Hello."]), _tts_failing_on("Let me check"))

    chunks = [c async for c in handler._synthesize_sentence_stream("Let me check that for you.", "s1", cache=True)]

    assert chunks == []
    assert handler._session("s1").unspoken_sentences == []


_SPLIT_REPLIES = [
    (["Your", " order", "_status", " is", " ready", "."], "Your order_status is ready."),
    (["Mail", " john", "_", "doe", "@x.com", "."], "Mail john_doe@x.com."),
    (["Code", " ABC", "_", "123", "."], "Code ABC_123."),
    (["I", " use", " C", "#", "."], "I use C#."),
    (["Ticket", " #", "42", "."], "Ticket #42."),
    (["It", " is", " **", "done", "**", "."], "It is done."),
]


async def test_token_split_underscores_and_hashes_are_spoken_and_stored_the_same():
    """#180: the live path feeds the parser one LLM token at a time; a token usually starts at the _ or #."""
    for tokens, expected in _SPLIT_REPLIES:
        spoken = []
        tts = MagicMock()

        async def _stream(text, sample_rate, _spoken=spoken):
            _spoken.append(text)
            yield b"\x00" * 320

        tts.synthesize_stream = _stream
        transcripts = MagicMock()
        handler = _make_handler(_make_stt("hi"), _make_llm(tokens), tts, transcripts=transcripts)

        async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
            pass

        assert spoken[-1] == expected   # earlier entries may be a filler phrase
        assert _reply_recorded(transcripts) == expected


async def test_a_failed_sentence_with_a_split_underscore_is_not_stored():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("hi"),
        _make_llm(["Your", " order", "_status", " FAILME", " is", " ready.", " Anything", " else?"]),
        _tts_failing_on("FAILME"), transcripts=transcripts,
    )

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert _reply_recorded(transcripts) == "Anything else?"


def _tts_silent_on(marker: str) -> MagicMock:
    """Returns no audio, without raising, for text containing marker (macOS `say` on a leading '-')."""
    tts = MagicMock()

    async def _stream(text, sample_rate):
        if marker in text:
            return
        yield b"\x00" * 320

    tts.synthesize_stream = _stream
    return tts


async def test_a_sentence_whose_tts_returned_no_audio_without_raising_is_not_stored_as_spoken():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("weather"), _make_llm(["-5 degrees outside today.", " Anything else?"]),
        _tts_silent_on("-5 degrees"), transcripts=transcripts,
    )

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert _reply_recorded(transcripts) == "Anything else?"
    assert handler._get_history("s1")[-1].content == "Anything else?"


async def test_empty_audio_chunks_do_not_count_as_audio():
    tts = MagicMock()

    async def _stream(text, sample_rate):
        yield b""
        yield b""

    tts.synthesize_stream = _stream
    handler = _make_handler(_make_stt("hi"), _make_llm(["Hello."]), tts)

    chunks = [c async for c in handler._synthesize_sentence_stream("Hello.", "s1", in_reply=True)]

    assert chunks == []
    assert handler._session("s1").unspoken_sentences == ["Hello."]


async def test_a_reply_that_is_entirely_silent_is_not_added_to_history(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "60000")
    handler = _make_handler(_make_stt("hi"), _make_llm(["-5 degrees."]), _tts_silent_on("-5"))

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0)]

    assert not any(r.tts_payloads or r.response_text for r in responses)
    assert [m.role for m in handler._get_history("s1")] == ["system"]


async def test_a_silent_cached_phrase_is_not_cached_and_is_not_a_reply_sentence():
    handler = _make_handler(_make_stt("hi"), _make_llm(["Hello."]), _tts_silent_on("Let me check"))

    chunks = [c async for c in handler._synthesize_sentence_stream("Let me check that.", "s1", cache=True)]

    assert chunks == []
    assert handler._session("s1").unspoken_sentences == []
