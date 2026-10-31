"""What the stored transcript calls spoken is what the servicer actually sent the caller (or, once the gateway
reports it, what was played), and a hang-up during a transfer-failed apology is persisted by teardown, before
the session is ended."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer
from services.conversation.session import ConversationSession, SessionContext

from .test_pipeline import RecordingEventBus, _make_handler, _make_llm, _make_stt, _make_tts, _silence


_TURN_NUMBER = 7


def _open() -> pb.GatewayMessage:
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="t1",
        routing_status=pb.ROUTING_STATUS_ROUTED,
    ))


def _recording_transcripts() -> tuple[MagicMock, asyncio.Event]:
    transcripts = MagicMock()
    recorded = asyncio.Event()

    def record_turn(*args, **kwargs):
        recorded.set()
        return _TURN_NUMBER

    transcripts.record_turn.side_effect = record_turn
    return transcripts, recorded


async def test_a_barge_in_drained_during_synthesis_stores_only_the_sentence_that_was_sent(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts, recorded = _recording_transcripts()
    tts = _make_tts()
    first_audio = tts.synthesize.return_value
    second_started, release_second = asyncio.Event(), asyncio.Event()

    async def synthesize(text, sample_rate):
        if "two" in text:
            second_started.set()
            await release_second.wait()
        return first_audio

    tts.synthesize = synthesize
    handler = _make_handler(
        _make_stt("hello"), _make_llm(["Sentence one. ", "Sentence two."]), tts, transcripts=transcripts,
    )
    gateway: asyncio.Queue = asyncio.Queue()

    async def stream():
        yield _open()
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        yield await gateway.get()

    async def factory(ctx):
        return handler

    async def consume():
        sent_one = False
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            if msg.HasField("tts_chunk") and not sent_one:
                sent_one = True
                await second_started.wait()
                # The barge-in lands while sentence two is still being synthesized.
                gateway.put_nowait(pb.GatewayMessage(
                    cancel_generation=pb.CancelGeneration(session_id="s1", trace_id="tr")))
                await asyncio.sleep(0.05)
                release_second.set()
            if recorded.is_set():
                return

    await asyncio.wait_for(consume(), 2)

    args = transcripts.record_turn.call_args.args
    assert args[3] == "Sentence one."
    assert args[4] is True


async def test_a_hangup_during_the_transfer_failed_apology_is_persisted_before_the_session_ends():
    transcripts, recorded = _recording_transcripts()
    handler = _make_handler(_make_stt(), _make_llm([]), _make_tts(), transcripts=transcripts)

    async def stream():
        yield _open()
        yield pb.GatewayMessage(transfer_failed=pb.TransferFailed(
            session_id="s1", destination="+15551234567", reason="esl_unreachable", transfer_id="x1"))
        await asyncio.Event().wait()   # the gateway holds the stream open until it hangs up

    async def factory(ctx):
        return handler

    converse = ConversationServicer(factory).Converse(stream(), AsyncMock())
    async for msg in converse:
        if msg.HasField("tts_chunk"):
            await converse.aclose()   # hung up while the servicer waits at the apology's yield
            break
    await asyncio.sleep(0.05)   # a finalizer that outlived teardown would have run by now

    assert recorded.is_set()
    writes = [c[0] for c in transcripts.method_calls if c[0] in ("record_turn", "end_call")]
    assert writes == ["record_turn", "end_call"]
    assert transcripts.record_turn.call_args.args[1] == "[transfer_failed: esl_unreachable]"
    assert "s1" not in handler._sessions


async def test_a_hangup_during_the_refused_transfer_apology_is_persisted_before_the_session_ends(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts, recorded = _recording_transcripts()
    replies = iter([["Connecting you. ", '[[TRANSFER reason="x"]]'], ["Sorry, I cannot transfer you."]])

    async def generate(messages):
        for token in next(replies):
            yield token

    llm = MagicMock()
    llm.generate = generate
    handler = _make_handler(_make_stt("transfer me"), llm, _make_tts(), transfer_type="none", transcripts=transcripts)
    # With the request stream already finished and no workflow to wait on, teardown has no await that
    # would give an unclosed apology's finalizer a turn before end_call().
    handler._finish_workflow = AsyncMock()
    gateway_closed = asyncio.Event()

    async def stream():
        yield _open()
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await gateway_closed.wait()

    async def factory(ctx):
        return handler

    converse = ConversationServicer(factory).Converse(stream(), AsyncMock())
    async for msg in converse:
        if msg.HasField("assistant_response"):
            gateway_closed.set()
            await asyncio.sleep(0.01)   # lets the servicer's reader see the stream end
            await converse.aclose()   # hung up with the turn prefetched to the nested apology's yield
            break
    await asyncio.sleep(0.05)   # a finalizer that outlived teardown would have run by now

    assert recorded.is_set()
    writes = [c[0] for c in transcripts.method_calls if c[0] in ("record_turn", "end_call")]
    assert writes[-2:] == ["record_turn", "end_call"]
    assert transcripts.record_turn.call_args.args[1] == "[transfer_failed: transfer_disabled]"
    assert "s1" not in handler._sessions


async def _barge_in_on_sentence_two(monkeypatch, *, report, report_first=False):
    """Barge in while sentence two is still synthesizing, then optionally send the gateway's PlaybackFinished
    (played_bytes=None: the field absent, as from an older gateway) before or after the turn is recorded.
    report=False sends none, as on a hang-up. Returns the transcripts mock."""
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")   # no filler: the first tts_chunk is sentence one
    transcripts, recorded = _recording_transcripts()
    tts = _make_tts()   # 320 bytes of audio per sentence
    second_started, release_second = asyncio.Event(), asyncio.Event()

    async def synthesize(text, sample_rate):
        if "two" in text:
            second_started.set()
            await release_second.wait()
        return b"\x00" * 320

    tts.synthesize = synthesize
    handler = _make_handler(
        _make_stt("hello"), _make_llm(["Sentence one. ", "Sentence two."]), tts, transcripts=transcripts,
    )
    gateway: asyncio.Queue = asyncio.Queue()

    async def stream():
        yield _open()
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        while (msg := await gateway.get()) is not None:
            yield msg

    async def factory(ctx):
        return handler

    def playback_finished():
        pf = pb.PlaybackFinished(session_id="s1", interrupted=True)
        if report is not None:
            pf.played_bytes = report
        return pb.GatewayMessage(playback_finished=pf)

    async def end_after_record():
        await recorded.wait()
        if not report_first and report is not False:
            gateway.put_nowait(playback_finished())
            await asyncio.sleep(0.05)
        gateway.put_nowait(None)

    async def consume():
        cut = False
        ender = asyncio.create_task(end_after_record())
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            if msg.HasField("tts_chunk") and not cut:
                cut = True
                await second_started.wait()
                gateway.put_nowait(pb.GatewayMessage(
                    cancel_generation=pb.CancelGeneration(session_id="s1", trace_id="tr")))
                await asyncio.sleep(0.05)
                if report_first:
                    gateway.put_nowait(playback_finished())
                    await asyncio.sleep(0.05)
                release_second.set()
        await ender

    await asyncio.wait_for(consume(), 2)
    return transcripts


async def test_a_report_that_lands_before_the_cut_turn_is_recorded_stores_the_heard_text(monkeypatch):
    # 160 of sentence one's 320 bytes were played: one of its two words.
    transcripts = await _barge_in_on_sentence_two(monkeypatch, report=160, report_first=True)

    assert transcripts.record_turn.call_args.args[3] == "Sentence"
    transcripts.correct_turn_reply.assert_not_called()


async def test_a_report_that_lands_after_the_cut_turn_is_recorded_corrects_the_row(monkeypatch):
    transcripts = await _barge_in_on_sentence_two(monkeypatch, report=160)

    assert transcripts.record_turn.call_args.args[3] == "Sentence one."
    transcripts.correct_turn_reply.assert_called_once_with(
        "s1", _TURN_NUMBER, "Sentence", newly_interrupted=False, count_barge_in=False)


async def test_a_report_covering_all_the_sent_audio_leaves_the_row_alone(monkeypatch):
    transcripts = await _barge_in_on_sentence_two(monkeypatch, report=320)

    assert transcripts.record_turn.call_args.args[3] == "Sentence one."
    transcripts.correct_turn_reply.assert_not_called()


async def test_a_hangup_without_a_report_stays_sent_based(monkeypatch):
    transcripts = await _barge_in_on_sentence_two(monkeypatch, report=False)

    assert transcripts.record_turn.call_args.args[3] == "Sentence one."
    transcripts.correct_turn_reply.assert_not_called()


async def test_an_older_gateway_without_played_bytes_stays_sent_based(monkeypatch):
    for report_first in (True, False):
        transcripts = await _barge_in_on_sentence_two(monkeypatch, report=None, report_first=report_first)

        assert transcripts.record_turn.call_args.args[3] == "Sentence one."
        transcripts.correct_turn_reply.assert_not_called()


# ── Reports about replies that finished streaming, and about the transfer-failed apology ────────────────

def _numbering_transcripts() -> MagicMock:
    transcripts = MagicMock()
    turns = iter(range(1, 100))
    transcripts.record_turn.side_effect = lambda *args, **kwargs: next(turns)
    return transcripts


async def _drain(gen, *, after_audio=None) -> None:
    """Consume a handler turn as the servicer does, marking each chunk sent. after_audio(n) runs once the
    n-th audio response (0-based) has been sent, i.e. while the reply is still being streamed."""
    n = 0
    async for response in gen:
        if response.on_sent:
            response.on_sent()
        if response.tts_payloads:
            if after_audio is not None:
                after_audio(n)
            n += 1


async def test_a_reply_that_finished_streaming_is_corrected_when_the_caller_cut_it(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts = _numbering_transcripts()
    handler = _make_handler(
        _make_stt("hello"), _make_llm(["Sentence one. ", "Sentence two."]), _make_tts(), transcripts=transcripts,
    )
    await _drain(handler.on_speech_ended("s1", _silence(), 1500, -20.0))
    assert transcripts.record_turn.call_args.args[3] == "Sentence one. Sentence two."
    assert transcripts.record_turn.call_args.args[4] is False

    # 480 of the 640 bytes sent were played: all of sentence one and one of sentence two's two words.
    handler.on_playback_interrupted("s1", 480)

    transcripts.correct_turn_reply.assert_called_once_with(
        "s1", 1, "Sentence one. Sentence", newly_interrupted=True, count_barge_in=True)


async def test_a_barge_in_after_the_reply_streamed_counts_once(monkeypatch):
    from services.conversation.transcript_builder import TranscriptBuilder

    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    builder = TranscriptBuilder(pool=MagicMock())
    builder._spawn = lambda session_id, coro: coro.close()
    builder.begin_call("s1", tenant_id="t1", call_id="c1")
    handler = _make_handler(
        _make_stt("hello"), _make_llm(["Sentence one. ", "Sentence two."]), _make_tts(), transcripts=builder,
    )
    await _drain(handler.on_speech_ended("s1", _silence(), 1500, -20.0))

    handler.on_playback_interrupted("s1", 480)

    assert builder._barge_in_counts["s1"] == 1


async def test_a_non_interrupted_playback_finished_leaves_the_row_alone(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts = _numbering_transcripts()
    handler = _make_handler(
        _make_stt("hello"), _make_llm(["Sentence one. ", "Sentence two."]), _make_tts(), transcripts=transcripts,
    )
    await _drain(handler.on_speech_ended("s1", _silence(), 1500, -20.0))

    handler.on_playback_completed("s1")
    handler.on_playback_interrupted("s1", 480)   # a stale count must not be applied afterwards

    transcripts.correct_turn_reply.assert_not_called()


_REFUSED = [["Connecting you. ", '[[TRANSFER reason="x"]]'], ["Sorry, I cannot transfer you."]]


def _refusing_handler(transcripts):
    replies = iter(_REFUSED)

    async def generate(messages):
        for token in next(replies):
            yield token

    llm = MagicMock()
    llm.generate = generate
    return _make_handler(_make_stt("transfer me"), llm, _make_tts(), transfer_type="none", transcripts=transcripts)


def _apology_turn(nested: bool, handler):
    """The generator that speaks the apology, and the index of its audio and the bytes played when the caller
    cut it after the first two of its five words. Nested: it follows the main reply's 320 bytes in one turn."""
    if nested:
        return handler.on_speech_ended("s1", _silence(), 1500, -20.0), 1, 320 + 160
    return handler.on_transfer_failed("s1", "+15551234567", "esl_unreachable"), 0, 160


@pytest.mark.parametrize("nested", [True, False])
async def test_a_cut_apology_is_stored_as_heard_when_the_report_lands_before_the_record(monkeypatch, nested):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts = _numbering_transcripts()
    handler = _refusing_handler(transcripts) if nested else _make_handler(
        _make_stt(), _make_llm(["Sorry, I cannot transfer you."]), _make_tts(), transcripts=transcripts)
    gen, index, played = _apology_turn(nested, handler)

    async def barge_in():
        await handler.on_cancel("s1")
        handler.on_playback_interrupted("s1", played)

    async def drain():
        n = 0
        async for response in gen:
            if response.on_sent:
                response.on_sent()
            if response.tts_payloads:
                if n == index:
                    await barge_in()
                n += 1

    await drain()

    args = transcripts.record_turn.call_args.args
    assert args[1].startswith("[transfer_failed")
    assert args[3:5] == ("Sorry, I", True)
    transcripts.correct_turn_reply.assert_not_called()


@pytest.mark.parametrize("nested", [True, False])
async def test_a_cut_apology_is_corrected_when_the_report_lands_after_the_record(monkeypatch, nested):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts = _numbering_transcripts()
    handler = _refusing_handler(transcripts) if nested else _make_handler(
        _make_stt(), _make_llm(["Sorry, I cannot transfer you."]), _make_tts(), transcripts=transcripts)
    gen, _, played = _apology_turn(nested, handler)
    await _drain(gen)
    args = transcripts.record_turn.call_args.args
    assert args[3:5] == ("Sorry, I cannot transfer you.", False)

    handler.on_playback_interrupted("s1", played)

    transcripts.correct_turn_reply.assert_called_once_with(
        "s1", transcripts.record_turn.call_count, "Sorry, I", newly_interrupted=True, count_barge_in=True)


async def test_a_cut_inside_the_main_reply_corrects_both_rows_and_counts_one_barge_in(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts = _numbering_transcripts()
    handler = _refusing_handler(transcripts)
    await _drain(handler.on_speech_ended("s1", _silence(), 1500, -20.0))
    assert transcripts.record_turn.call_args_list[0].args[3] == "Connecting you."

    # 160 of the main reply's 320 bytes were played: one of its two words, and none of the apology.
    handler.on_playback_interrupted("s1", 160)

    assert transcripts.correct_turn_reply.call_args_list == [
        call("s1", 1, "Connecting", newly_interrupted=True, count_barge_in=True),
        call("s1", 2, "", newly_interrupted=True, count_barge_in=False),
    ]


async def test_a_cut_inside_the_apology_leaves_the_main_row_alone(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts = _numbering_transcripts()
    handler = _refusing_handler(transcripts)
    await _drain(handler.on_speech_ended("s1", _silence(), 1500, -20.0))

    handler.on_playback_interrupted("s1", 320 + 160)

    assert transcripts.correct_turn_reply.call_args_list == [
        call("s1", 2, "Sorry, I", newly_interrupted=True, count_barge_in=True),
    ]


async def test_a_completed_playback_clears_the_correction_before_a_later_prompt_is_cut(monkeypatch):
    """Session level: the gateway's PlaybackFinished(false) for turn N must reach the handler, or a barge-in on
    an out-of-band prompt (call-flow timeout) would trim turn N's row by that prompt's byte count."""
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts = _numbering_transcripts()
    handler = _make_handler(
        _make_stt("hello"), _make_llm(["Sentence one. ", "Sentence two."]), _make_tts(), transcripts=transcripts,
    )
    session = ConversationSession(ctx=SessionContext(session_id="s1"), bus=RecordingEventBus(), handler=handler)
    session.session_ready()
    await _drain(handler.on_speech_ended("s1", _silence(), 1500, -20.0))

    session.on_playback_finished(False)
    session.on_playback_finished(True, 100)   # the barge-in on the timeout prompt

    transcripts.correct_turn_reply.assert_not_called()


async def test_a_cut_into_the_main_reply_while_the_apology_generates_counts_one_barge_in(monkeypatch):
    from services.conversation.transcript_builder import TranscriptBuilder

    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    builder = TranscriptBuilder(pool=MagicMock())
    builder._spawn = lambda session_id, coro: coro.close()
    builder.begin_call("s1", tenant_id="t1", call_id="c1")
    handler = _refusing_handler(builder)
    sent = 0

    async def barge_in_during_apology():
        # The report lands, inside the main reply's 320 bytes, before the cancelled apology is recorded.
        await handler.on_cancel("s1")
        handler.on_playback_interrupted("s1", 160)

    async for response in handler.on_speech_ended("s1", _silence(), 1500, -20.0):
        if response.on_sent:
            response.on_sent()
        if response.tts_payloads:
            sent += 1
            if sent == 2:
                await barge_in_during_apology()

    assert builder._turn_counts["s1"] == 2
    assert builder._barge_in_counts["s1"] == 1
