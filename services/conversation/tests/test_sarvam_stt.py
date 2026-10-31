"""SarvamSTT: httpx.MockTransport for REST and a fake `_ws_connect` for the live socket — no network, no cost."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from services.conversation.providers.stt.sarvam import SarvamSTT


class _FakeWs:
    """Scripted Sarvam socket: `on_flush` messages answer a flush; anything in `incoming` arrives at once."""

    def __init__(self, incoming: list[dict] | None = None, on_flush: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._q: asyncio.Queue = asyncio.Queue()
        self._on_flush = on_flush or []
        self.closed = False
        for m in incoming or []:
            self._q.put_nowait(json.dumps(m))

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("type") == "flush":
            for m in self._on_flush:
                self._q.put_nowait(json.dumps(m))

    def push(self, msg: dict) -> None:
        self._q.put_nowait(json.dumps(msg))

    async def close(self) -> None:
        self.closed = True
        self._q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item


def _stt(ws: _FakeWs | None = None, handler=None, **kw) -> tuple[SarvamSTT, list[str]]:
    stt = SarvamSTT(api_key="test-key", **kw)
    urls: list[str] = []

    async def connect(url, additional_headers=None):
        assert additional_headers == {"Api-Subscription-Key": "test-key"}
        urls.append(url)
        return ws

    stt._ws_connect = connect
    if handler:
        stt._client = httpx.AsyncClient(headers={"api-subscription-key": "test-key"}, transport=httpx.MockTransport(handler))
    return stt, urls


def _data(text: str, lang: str | None = None, prob: float | None = None) -> dict:
    return {"type": "data", "data": {"request_id": "r", "transcript": text, "language_code": lang,
                                     "language_probability": prob, "metrics": {}}}


START = {"type": "events", "data": {"signal_type": "START_SPEECH"}}


async def test_rest_transcribe_sends_wav_and_reports_detected_language():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["api-subscription-key"] == "test-key"
        body = request.content
        assert b'name="language_code"\r\n\r\nunknown' in body and b"RIFF" in body
        return httpx.Response(200, json={"transcript": " namaste ", "language_code": "hi-IN", "language_probability": 0.9})

    stt, _ = _stt(handler=handler)
    result = await stt.transcribe(b"\x00\x01" * 160, 16000)
    assert (result.text, result.language, result.language_confidence) == ("namaste", "hi", 0.9)


async def test_a_forced_language_is_sent_as_an_indian_locale_and_not_reported_as_detected():
    def handler(request: httpx.Request) -> httpx.Response:
        assert b'name="language_code"\r\n\r\nen-IN' in request.content
        return httpx.Response(200, json={"transcript": "hello", "language_code": "en-IN"})

    stt, _ = _stt(handler=handler, language="en")
    result = await stt.transcribe(b"\x00\x01" * 160, 16000)
    assert (result.text, result.language) == ("hello", None)


async def test_rest_errors_return_empty_text():
    stt, _ = _stt(handler=lambda r: httpx.Response(429, json={"error": {"message": "quota"}}))
    assert (await stt.transcribe(b"\x00\x01" * 160, 16000)).text == ""


async def test_streamed_audio_is_base64_pcm_and_a_finished_utterance_needs_no_flush():
    ws = _FakeWs()
    stt, urls = _stt(ws)
    await stt.feed_stream("s1", b"\x01\x02" * 80, 16000)
    assert "model=saaras%3Av4" in urls[0] and "input_audio_codec=pcm_s16le" in urls[0] and "mode=" not in urls[0]
    assert base64.b64decode(ws.sent[0]["audio"]["data"]) == b"\x01\x02" * 80
    ws.push(START)
    ws.push(_data("haan ji", "hi-IN", 0.8))
    await asyncio.sleep(0.01)
    result = await stt.finalize_stream("s1", b"", 16000)
    assert (result.text, result.language) == ("haan ji", "hi")
    assert not [m for m in ws.sent if m.get("type") == "flush"]
    await stt.cancel_stream("s1")
    assert ws.closed


async def test_speech_still_pending_is_flushed_and_waited_for():
    ws = _FakeWs(on_flush=[_data("book a table")])
    stt, _ = _stt(ws)
    await stt.feed_stream("s1", b"\x00\x00" * 80, 16000)
    ws.push(START)
    await asyncio.sleep(0.01)
    result = await stt.finalize_stream("s1", b"\x00\x00" * 80, 16000)
    assert result.text == "book a table"
    assert {"type": "flush"} in ws.sent
    # The connection stays for the next utterance, which starts clean.
    ws.push(_data("thanks"))
    await asyncio.sleep(0.01)
    assert (await stt.finalize_stream("s1", b"", 16000)).text == "thanks"


async def test_without_a_live_stream_the_buffered_audio_goes_over_rest():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"transcript": "fallback"})

    stt, _ = _stt(handler=handler)
    assert (await stt.finalize_stream("s1", b"\x00\x01" * 160, 16000)).text == "fallback"
    assert len(calls) == 1


async def test_rates_sarvam_cannot_stream_skip_the_socket():
    ws = _FakeWs()
    stt, urls = _stt(ws, handler=lambda r: httpx.Response(200, json={"transcript": "ok"}))
    await stt.feed_stream("s1", b"\x00\x00" * 80, 24000)
    assert urls == []
    assert (await stt.finalize_stream("s1", b"\x00\x00" * 80, 24000)).text == "ok"


async def test_output_mode_is_sent_for_saaras_v3_only():
    ws = _FakeWs()
    stt, urls = _stt(ws, model="saaras:v3", mode="codemix")
    await stt.prewarm_stream("s1", 8000)
    assert "mode=codemix" in urls[0] and "sample_rate=8000" in urls[0]
    await stt.cancel_stream("s1")
    stt4, urls4 = _stt(_FakeWs(), model="saaras:v4", mode="codemix")
    await stt4.prewarm_stream("s2", 16000)
    assert "mode=" not in urls4[0]
    await stt4.cancel_stream("s2")
    with pytest.raises(ValueError):
        SarvamSTT(api_key="k", mode="shout")
