"""CancelGeneration and later caller speech must be read while a turn is blocked on the LLM or a
tool, not only between the turn's yielded responses."""
import asyncio
from unittest.mock import AsyncMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer
from services.conversation.session import HandlerResponse


def _open():
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="t1",
        routing_status=pb.ROUTING_STATUS_ROUTED,
    ))


def _speech_ended():
    return pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=500))


def _cancel():
    return pb.GatewayMessage(cancel_generation=pb.CancelGeneration(session_id="s1", trace_id="tr"))


def _servicer(on_speech_ended):
    handler = AsyncMock()
    handler.out_responses = None
    handler.on_speech_ended = on_speech_ended

    async def factory(ctx):
        return handler

    return ConversationServicer(factory), handler


async def _run(servicer, messages, until, timeout=1.0):
    """Send `messages`, collect ServiceMessage payload names until `until(names)` or timeout."""
    async def stream():
        for msg in messages:
            yield msg
        await asyncio.Event().wait()   # gateway keeps the stream open

    names: list[str] = []
    done = asyncio.Event()

    async def consume():
        async for out in servicer.Converse(stream(), AsyncMock()):
            names.append(out.WhichOneof("payload"))
            if until(names):
                done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return names


async def test_cancel_is_acked_while_turn_is_blocked_on_llm():
    async def blocked_turn(session_id, audio, duration_ms, energy_db):
        await asyncio.Event().wait()   # LLM / tool never answers
        yield HandlerResponse()

    servicer, handler = _servicer(blocked_turn)
    names = await _run(servicer, [_open(), _speech_ended(), _cancel()], lambda n: "cancel_ack" in n)

    assert "cancel_ack" in names
    handler.on_cancel.assert_awaited_once()
    handler.on_session_end.assert_awaited_once()


async def test_speech_during_blocked_turn_is_handled_after_it_in_order():
    turns = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        turns.append(len(turns))
        if len(turns) == 1:
            await release.wait()
        yield HandlerResponse(stt_text=f"turn{len(turns)}")

    servicer, _ = _servicer(turn)

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.05)
        yield _speech_ended()   # arrives while turn 1 is still blocked
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.Event().wait()

    texts: list[str] = []
    done = asyncio.Event()

    async def consume():
        async for out in servicer.Converse(stream(), AsyncMock()):
            if out.WhichOneof("payload") == "stt_result" and out.stt_result.text:
                texts.append(out.stt_result.text)
                if len(texts) == 2:
                    done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert texts == ["turn1", "turn2"]


async def test_cancel_behind_a_deferred_speech_ended_still_stops_the_running_turn():
    """speech_ended, speech_ended, cancel_generation while turn 1 streams: the cancel belongs to
    turn 1 (acked now, no further audio, no end_call), and turn 2 is not hit by a stale cancel."""
    turns = []

    async def turn(session_id, audio, duration_ms, energy_db):
        turns.append(len(turns))
        if len(turns) == 2:
            yield HandlerResponse(stt_text="turn2")
            return
        yield HandlerResponse(stt_text="turn1")
        for i in range(6):
            await asyncio.sleep(0.05)
            yield HandlerResponse(tts_payloads=[b"\x00\x01"], response_text=f"s{i}")
        yield HandlerResponse(end_call=True)

    servicer, _ = _servicer(turn)

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.07)
        yield _speech_ended()
        yield _cancel()
        await asyncio.Event().wait()

    events: list[tuple[str, str]] = []
    done = asyncio.Event()

    async def consume():
        async for out in servicer.Converse(stream(), AsyncMock()):
            name = out.WhichOneof("payload")
            events.append((name, out.stt_result.text if name == "stt_result" else ""))
            if ("stt_result", "turn2") in events:
                done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 3)
        await asyncio.sleep(0.3)   # let a late end_call surface
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    names = [n for n, _ in events]
    assert "cancel_ack" in names
    assert names.count("tts_chunk") < 6
    assert "end_call" not in names
    assert ("stt_result", "turn1") in events and ("stt_result", "turn2") in events


async def test_frames_behind_a_deferred_speech_ended_are_examined_once_per_turn(monkeypatch):
    """With a speech_ended deferred, every later frame wakes a drain. Each drain must look only at
    the new arrivals, not rescan everything deferred so far (O(n^2) per turn), and the deferred
    messages must still reach the outer loop in arrival order once the turn ends."""
    import types

    import services.conversation.servicer as servicer_mod

    pops = 0

    class CountingDeque(servicer_mod.collections.deque):
        def popleft(self):
            nonlocal pops
            pops += 1
            return super().popleft()

    monkeypatch.setattr(
        servicer_mod, "collections", types.SimpleNamespace(deque=CountingDeque),
    )

    n_frames = 200
    turns = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        turns.append(len(turns))
        if len(turns) == 1:
            await release.wait()
        yield HandlerResponse(stt_text=f"turn{len(turns)}")

    servicer, handler = _servicer(turn)
    handler.on_audio.return_value = HandlerResponse()

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.05)
        yield _speech_ended()   # deferred: turn 1 is still running
        for i in range(n_frames):
            yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(
                session_id="s1", payload=i.to_bytes(2, "big"),
            ))
            await asyncio.sleep(0)   # one drain per frame
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.Event().wait()

    texts: list[str] = []
    done = asyncio.Event()

    async def consume():
        async for out in servicer.Converse(stream(), AsyncMock()):
            if out.WhichOneof("payload") == "stt_result" and out.stt_result.text:
                texts.append(out.stt_result.text)
                if len(texts) == 2:
                    done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 3)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert texts == ["turn1", "turn2"]
    # Each message is popped a small constant number of times; a per-drain rescan pops
    # roughly n_frames^2 / 2 (about 20,000 here).
    assert pops < 4 * (n_frames + 2), pops


def _audio(payload: bytes):
    return pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=payload))


def _dtmf():
    return pb.GatewayMessage(dtmf=pb.DtmfDigit(session_id="s1", digit="5"))


async def _utterance_audio_per_turn(burst):
    """Turn 1 blocks while `burst` arrives; return the audio each turn was handed."""
    audios: list[bytes] = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        audios.append(audio)
        if len(audios) == 1:
            await release.wait()
        yield HandlerResponse(stt_text=f"turn{len(audios)}")

    servicer, handler = _servicer(turn)
    handler.on_audio.return_value = HandlerResponse()
    n_turns = sum(1 for m in burst if m.WhichOneof("payload") == "speech_ended") + 1

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.05)
        for msg in burst:
            yield msg
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.Event().wait()

    done = asyncio.Event()

    async def consume():
        async for _ in servicer.Converse(stream(), AsyncMock()):
            if len(audios) == n_turns:
                done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 3)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return audios


async def test_each_utterance_queued_during_a_reply_keeps_its_own_audio():
    """Five utterances during one reply, no cancel: every deferred utterance reaches the pipeline
    with the audio sent before its speech_ended, not discarded as 'trailing frames' of a turn
    that ran ahead of it."""
    audios = await _utterance_audio_per_turn([
        _speech_ended(),
        _audio(b"\x03\x03"), _speech_ended(),
        _audio(b"\x04\x04"), _speech_ended(),
        _dtmf(),
        _audio(b"\x05\x05"), _speech_ended(),
    ])

    assert audios == [b"", b"", b"\x03\x03", b"\x04\x04", b"\x05\x05"]


async def test_audio_behind_a_dtmf_flood_survives_the_deferral():
    audios = await _utterance_audio_per_turn(
        [_speech_ended()] + [_dtmf() for _ in range(500)] + [_audio(b"\x03\x03"), _speech_ended()],
    )

    assert audios == [b"", b"", b"\x03\x03"]


async def test_utterance_spoken_before_first_turn_returns_keeps_its_audio():
    """Issue #218: the second utterance's frames arrive while turn 1 runs and before any deferred
    speech_ended, so they were discarded as trailing frames and the turn got 0 bytes."""
    audios = await _utterance_audio_per_turn([_audio(b"\x01\x01"), _audio(b"\x02\x02"), _speech_ended()])

    assert audios == [b"", b"\x01\x01\x02\x02"]


async def test_trailing_frames_with_no_following_utterance_are_still_discarded():
    """Frames that no speech_ended follows must not leak into the next utterance."""
    audios: list[bytes] = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        audios.append(audio)
        if len(audios) == 1:
            await release.wait()
        yield HandlerResponse(stt_text="x")

    servicer, handler = _servicer(turn)
    handler.on_audio.return_value = HandlerResponse()
    done = asyncio.Event()

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.05)
        yield _audio(b"\x09\x09")          # trailing, no speech_ended behind it
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.sleep(0.05)
        yield _audio(b"\x07\x07")          # next utterance, after the turn ended
        yield _speech_ended()
        await asyncio.Event().wait()

    async def consume():
        async for _ in servicer.Converse(stream(), AsyncMock()):
            if len(audios) == 2:
                done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 3)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert audios == [b"", b"\x07\x07"]


async def test_pre_speech_ended_audio_is_capped_to_one_utterance():
    from services.conversation.session import MAX_UTTERANCE_BYTES

    big = b"\x01" * MAX_UTTERANCE_BYTES
    audios = await _utterance_audio_per_turn([_audio(big), _audio(b"\x02\x02"), _speech_ended()])

    assert audios[0] == b"" and len(audios[1]) <= MAX_UTTERANCE_BYTES
    assert audios[1].endswith(b"\x02\x02")


async def test_half_close_during_the_drain_still_transcribes_the_deferred_utterance():
    audios: list[bytes] = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        audios.append(audio)
        if len(audios) == 1:
            await release.wait()
        yield HandlerResponse(stt_text=f"turn{len(audios)}")

    servicer, handler = _servicer(turn)
    handler.on_audio.return_value = HandlerResponse()

    async def on_cancel(_sid, **_):
        release.set()      # a cancelled turn ends

    handler.on_cancel.side_effect = on_cancel

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.05)
        yield _audio(b"\x05\x05")
        yield _speech_ended()
        # generator returns: gateway half-closes

    async def consume():
        async for _ in servicer.Converse(stream(), AsyncMock()):
            pass

    await asyncio.wait_for(consume(), 3)
    assert audios == [b"", b"\x05\x05"]


async def test_audio_flood_behind_a_deferred_speech_ended_is_capped(monkeypatch):
    """Issue #201: a deferred speech_ended makes every later frame queue whole; the held audio
    of one utterance must stop at the utterance cap instead of growing with the flood."""
    from services.conversation import servicer as servicer_mod

    monkeypatch.setattr(servicer_mod, "MAX_UTTERANCE_BYTES", 100)
    audios = await _utterance_audio_per_turn(
        [_speech_ended()] + [_audio(b"\x07" * 10) for _ in range(500)] + [_speech_ended()],
    )

    assert audios[:2] == [b"", b""]
    assert audios[2] == b"\x07" * 100


async def test_cap_is_per_utterance_behind_a_deferred_speech_ended(monkeypatch):
    from services.conversation import servicer as servicer_mod

    monkeypatch.setattr(servicer_mod, "MAX_UTTERANCE_BYTES", 100)
    audios = await _utterance_audio_per_turn(
        [_speech_ended()]
        + [_audio(b"\x01" * 60)] * 3 + [_speech_ended()]
        + [_audio(b"\x02" * 60)] * 3 + [_speech_ended()],
    )

    assert audios[2:] == [b"\x01" * 120, b"\x02" * 120]


async def test_audio_under_the_cap_is_untouched(monkeypatch):
    audios = await _utterance_audio_per_turn(
        [_speech_ended(), _audio(b"\x03" * 1000), _speech_ended()],
    )

    assert audios[2] == b"\x03" * 1000


async def test_cancel_drops_pre_cancel_frames_so_the_barge_in_utterance_is_clean():
    """Frames buffered before a cancel_generation must not be replayed after the barge-in audio."""
    audios: list[bytes] = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        audios.append(audio)
        if len(audios) == 1:
            await release.wait()
        yield HandlerResponse(stt_text=f"turn{len(audios)}")

    servicer, handler = _servicer(turn)
    handler.on_audio.return_value = HandlerResponse()
    done = asyncio.Event()

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.05)
        yield _audio(b"\x09\x09")
        yield _cancel()
        yield _audio(b"\x05\x05")
        yield _speech_ended()
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.Event().wait()

    async def consume():
        async for _ in servicer.Converse(stream(), AsyncMock()):
            if len(audios) == 2:
                done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 3)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert audios[1] == b"\x05\x05"
