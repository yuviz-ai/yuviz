"""
DeepgramSTT tests: httpx.MockTransport for REST and a fake `_ws_connect` for the live
WebSocket — no network, no cost.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from services.conversation.providers.stt.deepgram import DeepgramSTT


def _make_stt(handler) -> DeepgramSTT:
    stt = DeepgramSTT(api_key="test-key")
    stt._client = httpx.AsyncClient(
        base_url="https://api.deepgram.com",
        headers={"Authorization": "Token test-key"},
        transport=httpx.MockTransport(handler),
    )
    return stt


async def test_transcribe_parses_transcript_and_confidence():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Token test-key"
        assert request.url.params["encoding"] == "linear16"
        assert request.url.params["sample_rate"] == "16000"
        return httpx.Response(200, json={
            "results": {"channels": [{"alternatives": [
                {"transcript": "hello world", "confidence": 0.92},
            ]}]},
        })

    stt = _make_stt(handler)
    result = await stt.transcribe(b"\x00\x01" * 100, 16000)

    assert result.text == "hello world"
    assert result.confidence == pytest.approx(0.92)


async def test_transcribe_empty_audio_short_circuits_without_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not make a request for empty audio")

    stt = _make_stt(handler)
    result = await stt.transcribe(b"", 16000)

    assert result.text == ""


async def test_transcribe_returns_empty_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    stt = _make_stt(handler)
    result = await stt.transcribe(b"\x00\x01" * 100, 16000)

    assert result.text == ""


async def test_transcribe_returns_empty_on_unexpected_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    stt = _make_stt(handler)
    result = await stt.transcribe(b"\x00\x01" * 100, 16000)

    assert result.text == ""


class _FakeLiveWs:
    """Fake live WebSocket. Scripted Results are delivered in order; a Finalize message is answered with a
    from_finalize Results, like Deepgram. The connection stays open until close() is called."""

    def __init__(self, incoming: list[dict], *, ack_finalize: bool = True) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        for message in incoming:
            self._q.put_nowait(json.dumps(message))
        self._ack_finalize = ack_finalize
        self.sent: list = []
        self.closed = False

    def push(self, message: dict) -> None:
        self._q.put_nowait(json.dumps(message))

    async def send(self, data):
        self.sent.append(data)
        if self._ack_finalize and isinstance(data, str) and json.loads(data).get("type") == "Finalize":
            self.push({"type": "Results", "is_final": True, "from_finalize": True,
                       "channel": {"alternatives": [{"transcript": ""}]}})

    async def close(self):
        self.closed = True
        self._q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item


def _make_streaming_stt(incoming: list[dict]) -> tuple[DeepgramSTT, _FakeLiveWs]:
    stt = DeepgramSTT(api_key="test-key")
    fake_ws = _FakeLiveWs(incoming)

    async def _fake_connect(url, **kwargs):
        assert url.startswith("wss://api.deepgram.com/v1/listen")
        assert "Token test-key" in kwargs["additional_headers"]["Authorization"]
        return fake_ws

    stt._ws_connect = _fake_connect
    return stt, fake_ws


async def test_feed_stream_then_finalize_returns_concatenated_final_segments():
    stt, fake_ws = _make_streaming_stt([
        {"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "hel"}]}},
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "hello", "confidence": 0.9}]}},
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "world", "confidence": 0.8}]}},
    ])

    await stt.feed_stream("s1", b"\x01\x02", 16000)
    await stt.feed_stream("s1", b"\x03\x04", 16000)
    result = await stt.finalize_stream("s1", b"unused", 16000)

    assert result.text == "hello world"
    assert result.confidence == pytest.approx(0.8)  # last is_final segment's confidence
    assert fake_ws.sent[0] == b"\x01\x02"
    assert fake_ws.sent[1] == b"\x03\x04"
    assert json.loads(fake_ws.sent[2]) == {"type": "Finalize"}
    # The connection outlives the utterance: it is flushed, not closed, so the next turn pays no connect time.
    assert not fake_ws.closed
    assert "s1" in stt._streams


async def test_finalize_stream_with_no_prior_feed_returns_empty_without_opening_a_connection():
    stt, fake_ws = _make_streaming_stt([])
    result = await stt.finalize_stream("never-fed", b"unused", 16000)

    assert result.text == ""
    assert fake_ws.sent == []  # never even connected


async def test_feed_stream_reuses_the_same_connection_across_chunks():
    connect_calls = []
    stt = DeepgramSTT(api_key="test-key")
    fake_ws = _FakeLiveWs([])

    async def _fake_connect(url, **kwargs):
        connect_calls.append(url)
        return fake_ws

    stt._ws_connect = _fake_connect

    await stt.feed_stream("s1", b"\x01", 16000)
    await stt.feed_stream("s1", b"\x02", 16000)
    await stt.feed_stream("s1", b"\x03", 16000)

    assert len(connect_calls) == 1
    assert fake_ws.sent == [b"\x01", b"\x02", b"\x03"]


async def test_cancel_stream_closes_the_connection_without_finalizing():
    stt, fake_ws = _make_streaming_stt([
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "hello"}]}},
    ])
    await stt.feed_stream("s1", b"\x01", 16000)

    await stt.cancel_stream("s1")

    assert fake_ws.closed
    # The stream was dropped, so a later finalize finds nothing.
    result = await stt.finalize_stream("s1", b"unused", 16000)
    assert result.text == ""


async def test_keepalive_sent_during_silence(monkeypatch):
    # Real interval is 8s — shrink it so the test doesn't actually wait.
    monkeypatch.setattr(
        "services.conversation.providers.stt.deepgram._KEEPALIVE_INTERVAL_S", 0.01,
    )
    stt, fake_ws = _make_streaming_stt([])

    await stt.feed_stream("s1", b"\x01", 16000)
    await asyncio.sleep(0.05)  # let the keepalive loop fire at least once

    assert json.loads(fake_ws.sent[-1]) == {"type": "KeepAlive"}

    await stt.cancel_stream("s1")


async def test_feed_stream_evicts_dead_connection_and_reconnects_on_next_chunk():
    connect_calls = []
    total_sends = {"count": 0}

    class ConnectionClosedError_stub(Exception):
        pass

    class _DyingWs(_FakeLiveWs):
        """First send raises like a server-closed connection."""

        async def send(self, data):
            total_sends["count"] += 1
            if total_sends["count"] == 1:
                raise ConnectionClosedError_stub()
            await super().send(data)

    async def _fake_connect(url, **kwargs):
        ws = _DyingWs([])
        connect_calls.append(ws)
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _fake_connect

    await stt.feed_stream("s1", b"\x01", 16000)  # opens conn #1, send raises, gets evicted
    assert len(connect_calls) == 1
    assert "s1" not in stt._streams

    await stt.feed_stream("s1", b"\x02", 16000)  # must open a FRESH connection, not reuse the dead one
    assert len(connect_calls) == 2
    assert connect_calls[1].sent == [b"\x02"]


async def test_two_sessions_get_independent_connections():
    connect_calls = []
    ws_by_session = {}

    async def _fake_connect(url, **kwargs):
        connect_calls.append(url)
        ws = _FakeLiveWs([
            {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": f"call-{len(connect_calls)}"}]}},
        ])
        ws_by_session[len(connect_calls)] = ws
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _fake_connect

    await stt.feed_stream("s1", b"\x01", 16000)
    await stt.feed_stream("s2", b"\x02", 16000)

    result1 = await stt.finalize_stream("s1", b"unused", 16000)
    result2 = await stt.finalize_stream("s2", b"unused", 16000)

    assert len(connect_calls) == 2
    assert result1.text == "call-1"
    assert result2.text == "call-2"


# ── Language (language=multi code-switching) ─────────────────────────────────

def _words(*pairs):
    return [{"word": w, "language": lang} for w, lang in pairs]


async def test_transcribe_multi_reads_detected_language_from_word_tags():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["language"] == "multi"
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{
            "transcript": "mujhe appointment chahiye", "confidence": 0.9,
            "languages": ["hi", "en"],
            "words": _words(("mujhe", "hi"), ("appointment", "en"), ("chahiye", "hi")),
        }]}]}})

    stt = _make_stt(handler)
    result = await stt.transcribe(b"\x00\x01" * 100, 16000, language="multi")

    assert result.language == "hi"
    assert result.language_confidence == pytest.approx(2 / 3)
    assert result.language_shares == pytest.approx({"hi": 2 / 3, "en": 1 / 3})


async def test_transcribe_with_forced_language_reports_no_detection():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["language"] == "en"
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [
            {"transcript": "hello", "confidence": 0.9, "words": _words(("hello", "en"))},
        ]}]}})

    result = await _make_stt(handler).transcribe(b"\x00\x01" * 100, 16000)
    assert result.language is None and result.language_confidence is None


def test_default_language_is_english_not_auto_detect():
    assert DeepgramSTT(api_key="k", language=None)._language == "en"
    assert DeepgramSTT(api_key="k")._language == "en"


async def test_live_multi_accumulates_word_languages_across_segments():
    stt, _fake_ws = _make_streaming_stt([
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{
            "transcript": "hello", "confidence": 0.9, "languages": ["en"], "words": _words(("hello", "en"))}]}},
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{
            "transcript": "कैसे हो आप", "confidence": 0.8, "languages": ["hi"],
            "words": _words(("कैसे", "hi"), ("हो", "hi"), ("आप", "hi"))}]}},
    ])
    urls: list[str] = []
    connect = stt._ws_connect

    async def _recording_connect(url, **kwargs):
        urls.append(url)
        return await connect(url, **kwargs)

    stt._ws_connect = _recording_connect
    await stt.feed_stream("s1", b"\x01\x02", 16000, language="multi")
    result = await stt.finalize_stream("s1", b"unused", 16000, language="multi")

    assert "language=multi" in urls[0]
    assert result.language == "hi"
    assert result.language_confidence == pytest.approx(0.75)


async def test_live_without_word_tags_falls_back_to_languages_list():
    stt, _fake_ws = _make_streaming_stt([
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{
            "transcript": "hola", "confidence": 0.9, "languages": ["es"]}]}},
    ])
    await stt.feed_stream("s1", b"\x01\x02", 16000, language="multi")
    result = await stt.finalize_stream("s1", b"unused", 16000)
    assert result.language == "es" and result.language_confidence == 1.0


async def test_underscore_language_tag_is_canonicalised_for_rest_and_live():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["language"] == "en-US"
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [
            {"transcript": "hello", "confidence": 0.9},
        ]}]}})

    await _make_stt(handler).transcribe(b"\x00\x01" * 100, 16000, language=" en_US ")
    assert DeepgramSTT(api_key="k", language="en_US")._language == "en-US"

    stt = DeepgramSTT(api_key="test-key")
    urls = []

    async def _fake_connect(url, **kwargs):
        urls.append(url)
        return _FakeLiveWs([])

    stt._ws_connect = _fake_connect
    await stt.feed_stream("s1", b"\x01\x02", 16000, language="en_US")
    assert "language=en-US" in urls[0]


# ── Persistent connection, prewarm, endpointing ──────────────────────────────

def _final(text, conf=0.9, **extra):
    return {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": text, "confidence": conf}]}, **extra}


async def test_one_connection_serves_consecutive_utterances_with_clean_text_each_time():
    connects = []
    ws = _FakeLiveWs([])

    async def _connect(url, **kwargs):
        connects.append(url)
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect

    await stt.feed_stream("s1", b"\x01", 16000)
    ws.push(_final("first question"))
    await asyncio.sleep(0)
    first = await stt.finalize_stream("s1", b"", 16000)

    await stt.feed_stream("s1", b"\x02", 16000)
    ws.push(_final("second one"))
    await asyncio.sleep(0)
    second = await stt.finalize_stream("s1", b"", 16000)

    assert (first.text, second.text) == ("first question", "second one")
    assert len(connects) == 1
    assert [m for m in ws.sent if isinstance(m, str) and json.loads(m)["type"] == "Finalize"].__len__() == 2
    assert not ws.closed
    await stt.cancel_stream("s1")


async def test_prewarm_opens_the_connection_before_any_audio_and_the_first_chunk_reuses_it():
    connects = []
    ws = _FakeLiveWs([])

    async def _connect(url, **kwargs):
        connects.append(url)
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect

    await stt.prewarm_stream("s1", 16000)
    assert len(connects) == 1 and ws.sent == []

    await stt.feed_stream("s1", b"\x01", 16000)
    assert len(connects) == 1 and ws.sent == [b"\x01"]
    await stt.cancel_stream("s1")


async def test_a_prewarm_racing_the_first_chunk_opens_only_one_connection():
    connects = []
    gate = asyncio.Event()
    ws = _FakeLiveWs([])

    async def _slow_connect(url, **kwargs):
        connects.append(url)
        await gate.wait()
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _slow_connect

    warm = asyncio.create_task(stt.prewarm_stream("s1", 16000))
    feed = asyncio.create_task(stt.feed_stream("s1", b"\x01", 16000))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(warm, feed)

    assert len(connects) == 1
    assert ws.sent == [b"\x01"]
    await stt.cancel_stream("s1")


async def test_prewarm_swallows_a_failed_connect():
    async def _boom(url, **kwargs):
        raise OSError("no route")

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _boom
    await stt.prewarm_stream("s1", 16000)  # must not raise
    assert "s1" not in stt._streams


async def test_finalize_uses_what_arrived_when_the_flush_acknowledgement_is_late(monkeypatch):
    monkeypatch.setattr("services.conversation.providers.stt.deepgram._FINALIZE_TIMEOUT_S", 0.05)
    ws = _FakeLiveWs([_final("already final")], ack_finalize=False)

    async def _connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    await asyncio.sleep(0)

    result = await stt.finalize_stream("s1", b"", 16000)

    assert result.text == "already final"
    await stt.cancel_stream("s1")


async def test_a_connection_closed_by_the_server_is_dropped_and_the_next_chunk_reconnects():
    connects = []
    sockets = []

    async def _connect(url, **kwargs):
        connects.append(url)
        sockets.append(_FakeLiveWs([]))
        return sockets[-1]

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    await sockets[0].close()          # server hangs up
    await asyncio.sleep(0.01)         # read loop ends and evicts
    assert "s1" not in stt._streams

    await stt.feed_stream("s1", b"\x02", 16000)
    assert len(connects) == 2
    await stt.cancel_stream("s1")


async def test_finalize_on_a_dead_connection_falls_back_to_rest_with_the_buffered_audio():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["bytes"] = request.content
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": "from rest", "confidence": 0.7}]}]}})

    stt = _make_stt(handler)
    result = await stt.finalize_stream("never-opened", b"\x00\x01" * 100, 16000)

    assert result.text == "from rest"
    assert seen["bytes"] == b"\x00\x01" * 100


async def test_finalize_whose_send_fails_evicts_and_falls_back_to_rest():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": "recovered", "confidence": 0.7}]}]}})

    class _BrokenOnFinalize(_FakeLiveWs):
        async def send(self, data):
            if isinstance(data, str) and json.loads(data).get("type") == "Finalize":
                raise OSError("closed")
            await super().send(data)

    stt = _make_stt(handler)
    ws = _BrokenOnFinalize([])

    async def _connect(url, **kwargs):
        return ws

    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    result = await stt.finalize_stream("s1", b"\x01" * 64, 16000)

    assert result.text == "recovered"
    assert "s1" not in stt._streams and ws.closed


async def test_endpointing_is_sent_only_when_configured():
    urls = []

    async def _connect(url, **kwargs):
        urls.append(url)
        return _FakeLiveWs([])

    plain = DeepgramSTT(api_key="test-key")
    plain._ws_connect = _connect
    await plain.feed_stream("s1", b"\x01", 16000)
    tuned = DeepgramSTT(api_key="test-key", endpointing_ms=350)
    tuned._ws_connect = _connect
    await tuned.feed_stream("s1", b"\x01", 16000)

    assert "endpointing" not in urls[0]
    assert "endpointing=350" in urls[1]
    await plain.cancel_stream("s1")
    await tuned.cancel_stream("s1")


async def test_a_different_language_reopens_the_connection():
    connects = []
    sockets = []

    async def _connect(url, **kwargs):
        connects.append(url)
        sockets.append(_FakeLiveWs([]))
        return sockets[-1]

    stt = DeepgramSTT(api_key="test-key", language="en")
    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    await stt.feed_stream("s1", b"\x02", 16000, language="hi")

    assert len(connects) == 2 and "language=hi" in connects[1]
    assert sockets[0].closed
    await stt.cancel_stream("s1")


async def test_finalize_skips_the_flush_when_deepgram_already_reported_the_endpoint():
    ws = _FakeLiveWs([])

    async def _connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    ws.push({**_final("what is article fifteen"), "speech_final": True})
    await asyncio.sleep(0.01)

    result = await stt.finalize_stream("s1", b"", 16000)

    assert result.text == "what is article fifteen"
    assert not [m for m in ws.sent if isinstance(m, str) and json.loads(m)["type"] == "Finalize"]
    await stt.cancel_stream("s1")


async def test_finalize_still_flushes_when_words_arrived_after_the_endpoint():
    ws = _FakeLiveWs([])

    async def _connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    ws.push({**_final("what is article fifteen"), "speech_final": True})
    ws.push({"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "and also"}]}})
    await asyncio.sleep(0.01)

    await stt.finalize_stream("s1", b"", 16000)

    assert [m for m in ws.sent if isinstance(m, str) and json.loads(m)["type"] == "Finalize"]
    await stt.cancel_stream("s1")


async def test_the_skip_does_not_leak_into_the_next_utterance():
    ws = _FakeLiveWs([])

    async def _connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    ws.push({**_final("first"), "speech_final": True})
    await asyncio.sleep(0.01)
    await stt.finalize_stream("s1", b"", 16000)

    await stt.feed_stream("s1", b"\x02", 16000)
    ws.push(_final("second"))          # a final segment but no endpoint yet
    await asyncio.sleep(0.01)
    result = await stt.finalize_stream("s1", b"", 16000)

    assert result.text == "second"
    assert len([m for m in ws.sent if isinstance(m, str) and json.loads(m)["type"] == "Finalize"]) == 1
    await stt.cancel_stream("s1")


async def test_a_late_flush_reply_cannot_leak_into_the_next_utterance(monkeypatch):
    monkeypatch.setattr("services.conversation.providers.stt.deepgram._FINALIZE_TIMEOUT_S", 0.05)
    sockets = [_FakeLiveWs([_final("first")], ack_finalize=False), _FakeLiveWs([])]
    connects = iter(sockets)

    async def _connect(url, **kwargs):
        return next(connects)

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect
    await stt.feed_stream("s1", b"\x01", 16000)
    await asyncio.sleep(0)
    assert (await stt.finalize_stream("s1", b"", 16000)).text == "first"
    assert sockets[0].closed                      # dropped, so its late reply has nowhere to land

    sockets[0].push(_final("late tail"))
    await stt.feed_stream("s1", b"\x01", 16000)   # a fresh connection for the next utterance
    sockets[1].push(_final("second"))
    await asyncio.sleep(0.01)
    assert (await stt.finalize_stream("s1", b"", 16000)).text == "second"
    await stt.cancel_stream("s1")


async def test_a_prewarm_still_connecting_when_the_call_ends_is_closed():
    ws = _FakeLiveWs([])
    gate = asyncio.Event()

    async def _connect(url, **kwargs):
        await gate.wait()
        return ws

    stt = DeepgramSTT(api_key="test-key")
    stt._ws_connect = _connect
    prewarm = asyncio.create_task(stt.prewarm_stream("s1", 16000))
    await asyncio.sleep(0)
    await stt.cancel_stream("s1")                 # the caller hung up before the connection opened
    gate.set()
    await prewarm

    assert ws.closed and "s1" not in stt._streams


async def test_regional_host_from_env(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_HOST", "api.in.deepgram.com")
    stt = DeepgramSTT(api_key="test-key")
    seen = []

    async def _fake_connect(url, **kwargs):
        seen.append(url)
        return _FakeLiveWs([])

    stt._ws_connect = _fake_connect
    await stt.feed_stream("s1", b"\x01\x02", 16000)
    assert seen[0].startswith("wss://api.in.deepgram.com/v1/listen?")
    assert str(stt._client.base_url) == "https://api.in.deepgram.com"
    await stt.cancel_stream("s1")


@pytest.mark.parametrize("value", ["", "evil.com/x?y=", "http://api.in.deepgram.com", "a b"])
async def test_invalid_or_empty_host_falls_back_to_global(monkeypatch, value):
    monkeypatch.setenv("DEEPGRAM_API_HOST", value)
    stt = DeepgramSTT(api_key="test-key")
    assert str(stt._client.base_url) == "https://api.deepgram.com"
    assert stt._live_ws_url == "wss://api.deepgram.com/v1/listen"
