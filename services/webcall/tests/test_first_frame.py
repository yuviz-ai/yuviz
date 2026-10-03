"""Unit tests of webcall's first-frame credential handshake, with a fake
Conversation stub and a fake WebSocket — no network, no database."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest

from services.webcall import __main__ as webcall
from services.webcall.__main__ import pb

SENTINEL = "SENTINEL-cred-9f3a"
CRED_FRAME = json.dumps({"type": "test_credential", "credential": SENTINEL})
# Malformed JSON that still contains the credential
TRUNCATED_FRAME = '{"type":"test_credential","credential":"' + SENTINEL


class FakeWs:
    def __init__(self, path: str, frames: list) -> None:
        self.request = SimpleNamespace(path=path)
        self._frames = list(frames)
        self.sent: list = []
        self.closed_with: tuple[int, str] | None = None

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        await asyncio.Event().wait()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def send(self, data) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


class FakeCall:
    """Records writes; replays one service_ready, then ends the stream."""

    def __init__(self) -> None:
        self.writes: list = []
        self.cancelled = False

    async def write(self, msg) -> None:
        self.writes.append(msg)

    def cancel(self) -> None:
        self.cancelled = True

    def __aiter__(self):
        return self._replies()

    async def _replies(self):
        session_open = next(w.session_open for w in self.writes if w.WhichOneof("payload") == "session_open")
        yield pb.ServiceMessage(service_ready=pb.ServiceReady(session_id=session_open.session_id))

    def session_opens(self) -> list:
        return [w.session_open for w in self.writes if w.WhichOneof("payload") == "session_open"]


@pytest.fixture
def conv(monkeypatch):
    call = FakeCall()

    class Channel:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(webcall.grpc.aio, "insecure_channel", lambda target: Channel())
    monkeypatch.setattr(
        webcall.pb_grpc, "ConversationServiceStub", lambda channel: SimpleNamespace(Converse=lambda: call),
    )
    return call


@pytest.fixture
def no_browser_loop(monkeypatch):
    """The first-frame handler must never hand a rejected connection to the
    browser->gRPC loop."""
    async def boom(*args, **kwargs):
        raise AssertionError("_browser_to_grpc reached on a rejected first frame")

    monkeypatch.setattr(webcall, "_browser_to_grpc", boom)


def _run(ws: FakeWs) -> None:
    asyncio.run(webcall._handle_connection(ws))


def _assert_sentinel_unlogged(caplog) -> None:
    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in repr(record.args)


TEST_PATH = "/?tenant=acme&agent=front-desk&test=1"
PLAIN_PATH = "/?tenant=acme&agent=front-desk"


def test_good_first_frame_carries_credential_into_session_open(conv, caplog):
    caplog.set_level(logging.DEBUG)
    ws = FakeWs(TEST_PATH, [CRED_FRAME])
    _run(ws)

    opens = conv.session_opens()
    assert len(opens) == 1
    assert opens[0].test_credential == SENTINEL
    assert ws.closed_with is None
    # session_open is the first and only write: written once, after the frame
    assert [w.WhichOneof("payload") for w in conv.writes] == ["session_open"]
    _assert_sentinel_unlogged(caplog)


def test_service_ready_frame_carries_session_id_of_session_open(conv):
    ws = FakeWs(TEST_PATH, [CRED_FRAME])
    _run(ws)

    ready = [json.loads(m) for m in ws.sent if isinstance(m, str)]
    ready = [m for m in ready if m["type"] == "service_ready"]
    assert len(ready) == 1
    assert ready[0]["session_id"]
    assert ready[0]["session_id"] == conv.session_opens()[0].session_id


@pytest.mark.parametrize("frame", [
    json.dumps({"type": "speech_ended", "credential": SENTINEL}),
    json.dumps([SENTINEL]),
    json.dumps({"type": "test_credential"}),
    TRUNCATED_FRAME,
    SENTINEL.encode(),
], ids=["wrong-type", "non-object", "no-credential", "malformed-json", "binary"])
def test_bad_first_frame_closes_1008_without_session_open(conv, no_browser_loop, caplog, frame):
    caplog.set_level(logging.DEBUG)
    ws = FakeWs(TEST_PATH, [frame])
    _run(ws)

    assert ws.closed_with is not None and ws.closed_with[0] == 1008
    assert conv.writes == []
    _assert_sentinel_unlogged(caplog)


def test_first_frame_timeout_after_real_5s_closes_1008(conv, no_browser_loop, caplog):
    caplog.set_level(logging.DEBUG)
    assert webcall.FIRST_FRAME_TIMEOUT_S == 5.0
    ws = FakeWs(TEST_PATH, [])
    start = time.monotonic()
    _run(ws)
    elapsed = time.monotonic() - start

    assert 4.9 <= elapsed < 8
    assert ws.closed_with is not None and ws.closed_with[0] == 1008
    assert conv.writes == []
    _assert_sentinel_unlogged(caplog)


@pytest.mark.parametrize("frame", [CRED_FRAME, TRUNCATED_FRAME], ids=["valid-json", "malformed-json"])
def test_credential_frame_without_test_flag_behaves_as_before(conv, caplog, frame):
    caplog.set_level(logging.DEBUG)
    ws = FakeWs(PLAIN_PATH, [frame])
    _run(ws)

    opens = conv.session_opens()
    assert len(opens) == 1
    assert opens[0].test_credential == ""
    assert ws.closed_with is None
    # the frame went down the ordinary control path and was not written as audio
    assert [w.WhichOneof("payload") for w in conv.writes] == ["session_open"]
    _assert_sentinel_unlogged(caplog)
