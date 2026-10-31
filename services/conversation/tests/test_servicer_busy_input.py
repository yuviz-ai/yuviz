"""Caller input held behind a busy turn (the bounded `servicer._HeldBacklog`), and a close while a turn is busy."""
import asyncio
import gc
import importlib.util
import logging
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.conversation.servicer as servicer_mod
from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.session import MAX_UTTERANCE_BYTES, HandlerResponse
from services.conversation.tests import test_servicer_turn_cancel as tc

FRAME = 64_000


def _audio(payload: bytes):
    return pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=payload))


def _speech_ended():
    return pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1"))


def _playback_finished(sid="s1", interrupted=False):
    return pb.GatewayMessage(playback_finished=pb.PlaybackFinished(session_id=sid, interrupted=interrupted))


def _transfer_failed(sid="s1"):
    return pb.GatewayMessage(transfer_failed=pb.TransferFailed(session_id=sid))


def _dtmf():
    return pb.GatewayMessage(dtmf=pb.DtmfDigit(session_id="s1", digit="5"))


def _utterances(msgs):
    """Split drained messages at each speech_ended into closed utterances (their audio) and the open tail."""
    closed, tail = [], b""
    for m in msgs:
        if m.HasField("audio_chunk"):
            tail += m.audio_chunk.payload
        elif m.HasField("speech_ended"):
            closed.append(tail)
            tail = b""
    return closed, tail


def _drain(backlog):
    out = []
    while backlog:
        out.append(backlog.popleft())
    return out


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ── _HeldBacklog ───────────────────────────────────────────────────────────────────────────────

def test_default_backlog_ceiling_and_count_limit():
    assert servicer_mod.MAX_BACKLOG_BYTES == 4 * MAX_UTTERANCE_BYTES
    assert servicer_mod.MAX_QUEUED_UTTERANCES == 3


def test_dropped_utterances_release_their_payloads_not_just_the_counter():
    """Pre-fix a dropped entry kept its message in the FIFO: 60 x 1 MB kept 60 MB alive under a 7 MB counter."""
    backlog = servicer_mod._HeldBacklog("s1")
    for i in range(60):
        backlog.append(_audio(bytes([i]) * 1_000_000))
        backlog.append(_speech_ended())

    def payload_bytes(entries):
        return sum(len(e.msg.audio_chunk.payload) for e in entries if e.msg is not None)

    retained = payload_bytes(backlog._items)
    assert retained <= backlog.queued_bytes <= servicer_mod.MAX_BACKLOG_BYTES
    assert retained <= servicer_mod.MAX_QUEUED_UTTERANCES * 1_000_000
    assert len(backlog._items) <= 2 * len(backlog) + 1  # dead entries are compacted away


def test_audio_flood_stays_under_the_ceiling_dropping_the_oldest_utterance_once(caplog):
    backlog = servicer_mod._HeldBacklog("s1")
    frame = b"x" * FRAME
    with caplog.at_level(logging.WARNING):
        for utterance in range(3):
            for _ in range(MAX_UTTERANCE_BYTES // FRAME):
                backlog.append(_audio(bytes([utterance]) * FRAME))
            backlog.append(_speech_ended())
        assert not _warnings(caplog)
        for _ in range(300_000_000 // FRAME):
            backlog.append(_audio(frame))
            assert backlog.queued_bytes <= servicer_mod.MAX_BACKLOG_BYTES
    # Three full closed utterances plus a full open one exceed the ceiling: the oldest closed one is
    # dropped, then the open one stops at its own cap. One WARNING each.
    assert len(_warnings(caplog)) == 2
    closed, tail = _utterances(_drain(backlog))
    assert [c[:1] for c in closed] == [b"\x01", b"\x02"]
    assert set(tail) == {ord("x")} and len(tail) == MAX_UTTERANCE_BYTES
    assert backlog.queued_bytes == 0


def test_count_limit_drops_the_oldest_utterances_and_keeps_controls(monkeypatch, caplog):
    monkeypatch.setattr(servicer_mod, "MAX_QUEUED_UTTERANCES", 3)
    backlog = servicer_mod._HeldBacklog("s1")
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            backlog.append(_audio(bytes([i]) * 10))
            if i == 0:
                backlog.append(_playback_finished())
            if i == 1:
                backlog.append(_transfer_failed())
            backlog.append(_speech_ended())
    assert len(_warnings(caplog)) == 1  # two utterances dropped, one WARNING per busy turn
    msgs = _drain(backlog)
    kinds = [m.WhichOneof("payload") for m in msgs]
    assert kinds[:2] == ["playback_finished", "transfer_failed"]
    closed, tail = _utterances(msgs)
    assert closed == [bytes([i]) * 10 for i in range(2, 5)] and tail == b""


def test_control_message_flood_is_bounded_and_logged_once(caplog):
    backlog = servicer_mod._HeldBacklog("s1")
    backlog.append(_speech_ended())
    with caplog.at_level(logging.WARNING):
        for _ in range(servicer_mod.MAX_BACKLOG_BYTES // 1000 * 3):
            backlog.append(_transfer_failed("x" * 1000))
            assert backlog.queued_bytes <= servicer_mod.MAX_BACKLOG_BYTES
    assert len(_warnings(caplog)) == 1
    assert backlog.queued_bytes > servicer_mod.MAX_BACKLOG_BYTES - 2000


def test_alternating_control_flood_then_tiny_utterances_log_one_audio_warning(caplog):
    """playback_finished alternating with speech_ended never collapses and fills the backlog; every later
    audio + speech_ended pair is a fresh utterance whose refused frame must not log its own line."""
    backlog = servicer_mod._HeldBacklog("s1")
    backlog.append(_speech_ended())
    # The count limit drops the empty utterances, so the playback_finished fill the whole ceiling.
    for i in range(servicer_mod.MAX_BACKLOG_BYTES // 256 + 100):
        backlog.append(_playback_finished(f"s{i}"))
        backlog.append(_speech_ended())
    assert backlog.queued_bytes > servicer_mod.MAX_BACKLOG_BYTES - servicer_mod._CONTROL_RESERVE_BYTES
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(1000):
            backlog.append(_audio(b"\x01" * 300))
            backlog.append(_speech_ended())
    assert len([m for m in _warnings(caplog) if "dropping audio" in m]) == 1
    assert len(_warnings(caplog)) <= 2  # plus at most the once-per-turn dropped-utterance line


def test_back_to_back_playback_finished_collapse_to_the_latest():
    backlog = servicer_mod._HeldBacklog("s1")
    backlog.append(_speech_ended())
    for i in range(1000):
        backlog.append(_playback_finished("x" * 1000, interrupted=i == 999))
    msgs = _drain(backlog)
    assert [m.WhichOneof("payload") for m in msgs] == ["speech_ended", "playback_finished"]
    assert msgs[1].playback_finished.interrupted
    assert backlog.queued_bytes == 0


def test_speech_ended_evicts_held_dtmf_at_the_minimum_ceiling(monkeypatch, caplog):
    monkeypatch.setattr(
        servicer_mod, "MAX_BACKLOG_BYTES", MAX_UTTERANCE_BYTES + servicer_mod._CONTROL_RESERVE_BYTES,
    )
    backlog = servicer_mod._HeldBacklog("s1")
    for _ in range(MAX_UTTERANCE_BYTES // FRAME):
        backlog.append(_audio(b"x" * FRAME))
    for _ in range(600):  # more than fit: the backlog is full
        backlog.append(_dtmf())
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        backlog.append(_speech_ended())
    assert backlog.queued_bytes <= servicer_mod.MAX_BACKLOG_BYTES
    assert len(_warnings(caplog)) == 1 and "dropped 1 oldest held dtmf" in _warnings(caplog)[0]
    closed, tail = _utterances(_drain(backlog))
    assert tail == b"" and len(closed) == 1 and set(closed[0]) == {ord("x")}


def test_empty_speech_ended_stream_logs_once_per_busy_turn(monkeypatch, caplog):
    monkeypatch.setattr(servicer_mod, "MAX_QUEUED_UTTERANCES", 3)
    backlog = servicer_mod._HeldBacklog("s1")
    with caplog.at_level(logging.WARNING):
        for _ in range(1000):
            backlog.append(_speech_ended())
        assert len(_warnings(caplog)) == 1
        backlog.popleft()  # the busy turn ended and the backlog started draining
        for _ in range(1000):
            backlog.append(_speech_ended())
    assert len(_warnings(caplog)) == 2
    assert [m.WhichOneof("payload") for m in _drain(backlog)] == ["speech_ended"] * 3


def test_speech_ended_still_closes_an_utterance_that_filled_the_backlog():
    backlog = servicer_mod._HeldBacklog("s1")
    for _ in range(4 * MAX_UTTERANCE_BYTES // FRAME):
        backlog.append(_audio(b"x" * FRAME))
    backlog.append(_speech_ended())
    assert backlog.queued_bytes <= servicer_mod.MAX_BACKLOG_BYTES
    assert [m.WhichOneof("payload") for m in _drain(backlog)][-1] == "speech_ended"


def test_alternating_control_flood_at_the_ceiling_costs_little_cpu(monkeypatch):
    """Dropping the oldest utterance must not re-walk the held control messages: an O(n) walk
    makes the flood quadratic."""
    monkeypatch.setattr(servicer_mod, "MAX_QUEUED_UTTERANCES", 0)  # the byte ceiling does the dropping
    backlog = servicer_mod._HeldBacklog("s1")
    for utterance in range(3):
        backlog.append(_audio(bytes([utterance]) * MAX_UTTERANCE_BYTES))
        backlog.append(_speech_ended())
    gc.disable()  # a full collection of the rest of the suite's objects is not the append's cost
    try:
        started = time.process_time()
        for i in range(40_000):
            backlog.append(_playback_finished(f"s{i}") if i % 2 == 0 else _speech_ended())
            assert backlog.queued_bytes <= servicer_mod.MAX_BACKLOG_BYTES
        total = time.process_time() - started
    finally:
        gc.enable()
    assert total < 2


def test_backlog_ceiling_below_its_floor_is_raised_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("VOICEAI_MAX_BACKLOG_BYTES", "10")
    spec = importlib.util.spec_from_file_location(
        "services.conversation._servicer_floor_probe", servicer_mod.__file__,
    )
    probe = importlib.util.module_from_spec(spec)
    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(probe)
    floor = MAX_UTTERANCE_BYTES + servicer_mod._CONTROL_RESERVE_BYTES
    assert probe.MAX_BACKLOG_BYTES == floor
    assert f"VOICEAI_MAX_BACKLOG_BYTES=10 is below its minimum; using {floor}" in _warnings(caplog)


# ── Converse: input held behind a busy turn ───────────────────────────────────────────────────

async def _audio_per_turn(burst, n_turns):
    """Turn 1 blocks while `burst` arrives; return the audio each of `n_turns` turns was handed."""
    audios: list[bytes] = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        audios.append(audio)
        if len(audios) == 1:
            await release.wait()
        yield HandlerResponse(stt_text=f"turn{len(audios)}")

    servicer, handler = tc._servicer(turn)
    handler.on_audio.return_value = HandlerResponse()

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.05)
        for msg in burst:
            yield msg
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.Event().wait()

    async def consume():
        async for _ in servicer.Converse(stream(), AsyncMock()):
            if len(audios) == n_turns:
                await asyncio.sleep(0.05)  # a turn beyond n_turns would show up here
                return

    await asyncio.wait_for(consume(), 3)
    return audios


async def test_speech_ended_after_300_dtmf_keeps_its_utterance_boundary(monkeypatch, caplog):
    monkeypatch.setattr(servicer_mod, "MAX_BACKLOG_BYTES", 40_000)
    monkeypatch.setattr(servicer_mod, "_CONTROL_RESERVE_BYTES", 1024)
    with caplog.at_level(logging.WARNING):
        audios = await _audio_per_turn(
            [tc._speech_ended(), _audio(b"\x0a" * 100)] + [_dtmf() for _ in range(300)]
            + [tc._speech_ended()],
            n_turns=3,
        )
    assert audios == [b"", b"", b"\x0a" * 100]
    messages = _warnings(caplog)
    assert len([m for m in messages if "dropping dtmf" in m]) == 1
    assert len([m for m in messages if "oldest held dtmf" in m]) == 1


async def test_backlog_ceiling_drops_the_oldest_utterance_in_order(monkeypatch, caplog):
    monkeypatch.setattr(servicer_mod, "MAX_BACKLOG_BYTES", 2_600 + 1024)
    monkeypatch.setattr(servicer_mod, "_CONTROL_RESERVE_BYTES", 1024)
    burst = [tc._speech_ended()]
    for i in range(1, 5):
        burst += [_audio(bytes([i]) * 1000), tc._speech_ended()]
    with caplog.at_level(logging.WARNING):
        audios = await _audio_per_turn(burst, n_turns=3)
    # Only one closed 1000-byte utterance and an open one fit below the reserve, so each new one
    # drops the oldest: the empty first utterance, then 1, then 2. One WARNING for the busy turn.
    assert audios == [b"", bytes([3]) * 1000, bytes([4]) * 1000]
    assert len([m for m in _warnings(caplog) if "dropped oldest queued utterance" in m]) == 1


async def test_count_limit_drops_the_oldest_utterance_in_order(monkeypatch, caplog):
    monkeypatch.setattr(servicer_mod, "MAX_QUEUED_UTTERANCES", 2)
    burst = []
    for i in range(4):
        burst += [_audio(bytes([i]) * 4), tc._speech_ended()]
    with caplog.at_level(logging.WARNING):
        audios = await _audio_per_turn(burst, n_turns=3)
    assert audios == [b"", bytes([2]) * 4, bytes([3]) * 4]
    assert len([m for m in _warnings(caplog) if "dropped oldest queued utterance" in m]) == 1


async def test_pre_speech_ended_audio_is_charged_its_wire_size(monkeypatch):
    """Empty-payload frames with a large trace_id cost nothing by payload length; pre-fix they piled up
    without bound behind a busy turn."""
    monkeypatch.setattr(servicer_mod, "MAX_UTTERANCE_BYTES", 100_000)
    peak = [0]

    class PeakDeque(servicer_mod.collections.deque):
        def append(self, item):
            super().append(item)
            if isinstance(item, pb.GatewayMessage):
                peak[0] = max(peak[0], sum(m.ByteSize() for m in self if isinstance(m, pb.GatewayMessage)))

    monkeypatch.setattr(servicer_mod, "collections", types.SimpleNamespace(deque=PeakDeque))
    frame = pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=b"", trace_id="t" * 10_000))
    audios = await _audio_per_turn([frame] * 200 + [tc._speech_ended()], n_turns=2)

    assert audios == [b"", b""]
    assert peak[0] <= 100_000 + 10_100


async def test_unexpected_messages_during_a_turn_log_once(caplog):
    with caplog.at_level(logging.WARNING):
        audios = await _audio_per_turn([_dtmf() for _ in range(200)] + [tc._speech_ended()], n_turns=2)
    assert audios == [b"", b""]
    assert len([m for m in _warnings(caplog) if "Unexpected dtmf" in m]) == 1


# ── Converse: a close while a turn is busy ────────────────────────────────────────────────────

def _servicer_for(on_speech_ended):
    servicer, handler = tc._servicer(on_speech_ended)
    handler.on_audio.return_value = HandlerResponse()
    return servicer, handler


async def _close_run(servicer, end_stream, raises=False):
    """Run Converse to its end; return the seconds from the end of the request stream to the return."""
    loop = asyncio.get_running_loop()
    closed_at = 0.0

    async def stream():
        nonlocal closed_at
        async for msg in end_stream():
            yield msg
        closed_at = loop.time()
        if raises:
            raise ConnectionError("webcall disconnected")

    async def consume():
        async for _ in servicer.Converse(stream(), AsyncMock()):
            pass

    await asyncio.wait_for(consume(), 10)
    return loop.time() - closed_at


@pytest.mark.parametrize("raises", [False, True], ids=["half_close", "iterator_raises"])
async def test_close_during_a_stalled_turn_closes_the_session_within_2s(raises):
    """Pre-fix the close waited for the stalled turn for ever (about 28 s in #203)."""
    baseline = asyncio.all_tasks()

    async def turn(session_id, audio, duration_ms, energy_db):
        await asyncio.Event().wait()
        yield HandlerResponse()

    servicer, handler = _servicer_for(turn)

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.2)

    elapsed = await _close_run(servicer, stream, raises)
    await asyncio.sleep(0)

    handler.on_session_end.assert_awaited_once()
    assert elapsed < 2.3  # 1.8 s close budget plus teardown, with margin for loaded CI
    assert asyncio.all_tasks() - baseline == set()


def _deferred_stt_stub(stt_s, persisted, replied):
    """Turn 1 stalls on the LLM. Later turns take `stt_s` to transcribe, then answer at once. Like the
    pipeline, a turn closed after its transcript persists that transcript."""
    entered: list[int] = []

    async def turn(session_id, audio, duration_ms, energy_db):
        entered.append(1)
        n = len(entered)
        if n == 1:
            await asyncio.Event().wait()
        text = f"utterance{n}"
        await asyncio.sleep(stt_s)
        try:
            yield HandlerResponse(stt_text=text)
            replied.append(text)
            yield HandlerResponse(tts_payloads=[b"\x00\x01"], response_text="reply")
        except (GeneratorExit, asyncio.CancelledError):
            persisted.append(text)
            raise

    return turn


async def test_deferred_utterance_is_transcribed_at_close_and_no_reply_is_generated():
    persisted: list[str] = []
    replied: list[str] = []
    servicer, handler = _servicer_for(_deferred_stt_stub(0, persisted, replied))

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.1)
        yield tc._audio(b"\x01\x01")
        yield tc._speech_ended()
        await asyncio.sleep(0.1)

    elapsed = await _close_run(servicer, stream)

    assert persisted == ["utterance2"]
    assert replied == []   # no assistant reply the caller never heard
    handler.on_session_end.assert_awaited_once()
    assert elapsed < 2.3  # 1.8 s close budget plus teardown, with margin for loaded CI


async def test_each_deferred_utterance_gets_its_own_drain_budget_at_close():
    persisted: list[str] = []
    replied: list[str] = []
    servicer, handler = _servicer_for(_deferred_stt_stub(0.6, persisted, replied))

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.1)
        for _ in range(2):
            yield tc._audio(b"\x01\x01")
            yield tc._speech_ended()
        await asyncio.sleep(0.1)

    elapsed = await _close_run(servicer, stream)

    assert persisted == ["utterance2", "utterance3"]
    assert replied == []
    handler.on_session_end.assert_awaited_once()
    budget = servicer_mod._CLOSE_DRAIN_TIMEOUT_S + servicer_mod._TURN_RELEASE_TIMEOUT_S
    assert elapsed < servicer_mod._CLOSE_GRACE_S + 3 * budget


async def test_a_deferred_turn_that_swallows_cancellation_is_abandoned_at_its_deadline(caplog):
    stop = asyncio.Event()
    entered: list[int] = []

    async def turn(session_id, audio, duration_ms, energy_db):
        entered.append(1)
        if len(entered) == 1:
            await asyncio.Event().wait()
        while not stop.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass   # a transcriber that swallows cancellation
        yield HandlerResponse()

    servicer, handler = _servicer_for(turn)

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.1)
        yield tc._audio(b"\x01\x01")
        yield tc._speech_ended()

    try:
        elapsed = await _close_run(servicer, stream)
    finally:
        stop.set()

    assert len(entered) == 2
    handler.on_session_end.assert_awaited_once()
    assert elapsed < 2.3  # 1.8 s close budget plus teardown, with margin for loaded CI
    assert any("abandoned" in m for m in _warnings(caplog))


async def test_twenty_utterances_queued_at_close_end_the_session_within_the_close_budget(monkeypatch, caplog):
    monkeypatch.setattr(servicer_mod, "MAX_QUEUED_UTTERANCES", 0)
    persisted: list[str] = []
    replied: list[str] = []
    stub = _deferred_stt_stub(0.3, persisted, replied)
    stt_calls: list[int] = []

    async def turn(*args):
        stt_calls.append(1)
        async for response in stub(*args):
            yield response

    servicer, handler = _servicer_for(turn)

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.1)
        for _ in range(20):
            yield tc._audio(b"\x01\x01")
            yield tc._speech_ended()
        await asyncio.sleep(0.1)

    with caplog.at_level(logging.WARNING):
        elapsed = await _close_run(servicer, stream)

    assert elapsed < 2.5
    assert len(stt_calls) <= 1 + int(servicer_mod._CLOSE_BUDGET_S / 0.3) + 1
    assert persisted and replied == []
    handler.on_session_end.assert_awaited_once()
    assert len([m for m in _warnings(caplog) if "close deadline reached, dropping" in m]) == 1


# ── A held transfer_failed is not replayed once the caller has gone ───────────────────────────

async def _held_transfer_failed_close(first_turn, stream):
    servicer, handler = _servicer_for(first_turn)

    async def stalled_apology(*args, **kwargs):
        await asyncio.Event().wait()
        yield HandlerResponse()

    handler.on_transfer_failed = MagicMock(side_effect=stalled_apology)
    handler.record_live_stage = MagicMock()  # synchronous on the real handler
    elapsed = await _close_run(servicer, stream)
    return handler, elapsed


async def test_held_transfer_failed_is_skipped_when_the_caller_hangs_up_during_a_stalled_turn():
    async def turn(session_id, audio, duration_ms, energy_db):
        await asyncio.Event().wait()
        yield HandlerResponse()

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.1)
        yield _transfer_failed()
        await asyncio.sleep(0.1)

    handler, elapsed = await _held_transfer_failed_close(turn, stream)

    handler.on_transfer_failed.assert_not_called()
    handler.on_session_end.assert_awaited_once()
    assert handler.on_session_end.await_args.args[1] in ("TRANSFER_FAILED", "TRANSFER_TIMEOUT")
    assert elapsed < 2.3  # 1.8 s close budget plus teardown, with margin for loaded CI


async def test_held_transfer_failed_is_skipped_when_the_caller_hangs_up_after_the_turn_was_drained():
    """The hang-up lands after the last drain and before the turn ends, so the turn never sees it;
    the held apology must still not run."""
    async def turn(session_id, audio, duration_ms, energy_db):
        await asyncio.sleep(0.1)
        yield HandlerResponse(stt_text="hello")   # the drain behind this response holds transfer_failed
        await asyncio.sleep(0.4)

    async def stream():
        yield tc._open()
        yield tc._speech_ended()
        await asyncio.sleep(0.05)
        yield _transfer_failed()
        await asyncio.sleep(0.2)

    handler, elapsed = await _held_transfer_failed_close(turn, stream)

    handler.on_transfer_failed.assert_not_called()
    handler.on_session_end.assert_awaited_once()
    assert handler.on_session_end.await_args.args[1] in ("TRANSFER_FAILED", "TRANSFER_TIMEOUT")
    assert elapsed < 2.3  # 1.8 s close budget plus teardown, with margin for loaded CI
