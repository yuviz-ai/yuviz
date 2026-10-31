"""A caller who streams audio without ever ending the utterance must not grow the session buffer
without bound."""
from unittest.mock import AsyncMock

from services.conversation.event_bus import EventBus
from services.conversation.fsm import CallFsmState
from services.conversation.session import (
    MAX_UTTERANCE_BYTES, ConversationSession, HandlerResponse, SessionContext,
)


async def _session() -> ConversationSession:
    handler = AsyncMock()
    handler.on_audio.return_value = AsyncMock(tts_payloads=[])
    session = ConversationSession(ctx=SessionContext(session_id="s1"), bus=EventBus(), handler=handler)
    session.session_ready()
    return session


async def test_buffer_is_capped_per_utterance(caplog):
    session = await _session()
    chunk = bytes(1_000_000)

    for _ in range(5):
        await session.push_audio(chunk)

    assert len(session._audio_buffer) == MAX_UTTERANCE_BYTES
    warnings = [r for r in caplog.records if "over cap" in r.getMessage()]
    assert len(warnings) == 1


async def test_buffer_accepts_a_new_utterance_after_the_cap_was_hit():
    session = await _session()
    await session.push_audio(bytes(MAX_UTTERANCE_BYTES + 1))
    await session.cancel()

    await session.push_audio(b"\x01\x02")

    assert bytes(session._audio_buffer) == b"\x01\x02"


async def test_cap_hit_exactly_on_a_frame_boundary_warns_once(caplog):
    session = await _session()
    frame = bytes(MAX_UTTERANCE_BYTES // 4)
    assert len(frame) * 4 == MAX_UTTERANCE_BYTES

    for _ in range(6):
        await session.push_audio(frame)

    assert len(session._audio_buffer) == MAX_UTTERANCE_BYTES
    warnings = [r for r in caplog.records if "over cap" in r.getMessage()]
    assert len(warnings) == 1


def _cap_warnings(caplog):
    return [r for r in caplog.records if "over cap" in r.getMessage()]


async def test_exact_cap_then_one_more_frame_warns_once(caplog):
    session = await _session()
    await session.push_audio(bytes(MAX_UTTERANCE_BYTES))
    await session.push_audio(b"\x01")

    assert len(session._audio_buffer) == MAX_UTTERANCE_BYTES
    assert len(_cap_warnings(caplog)) == 1


async def test_ignored_speech_ended_while_speaking_drops_the_buffered_audio(caplog):
    session = await _session()
    fsm = session._fsm
    fsm.on_speech_started()
    fsm.on_speech_ended()
    fsm.on_stt_final("hi")
    fsm.on_text_ready()
    fsm.on_first_audio_chunk()
    assert session.fsm_state is CallFsmState.SPEAKING

    await session.push_audio(bytes(MAX_UTTERANCE_BYTES + 1))
    async for _ in session.speech_ended(100, -30.0):
        pass
    fsm.on_playback_finished()
    assert session.fsm_state is CallFsmState.LISTENING

    handled = []

    async def _turn(session_id, audio, *_args):
        handled.append(audio)
        yield HandlerResponse()

    session._handler.on_speech_ended = _turn
    await session.push_audio(b"\x01" * (MAX_UTTERANCE_BYTES + 1))
    async for _ in session.speech_ended(100, -30.0):
        pass

    assert handled == [b"\x01" * MAX_UTTERANCE_BYTES]
    assert len(_cap_warnings(caplog)) == 2
