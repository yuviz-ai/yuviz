"""SarvamTTS: request shape, WAV decoding, resampling, language selection, failure handling."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave

import httpx
import numpy as np
import pytest

from libs.config_sdk.languages import tts_languages
from services.conversation.providers.tts.sarvam import SarvamTTS


def _wav(rate: int, samples: int = 160) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(np.arange(samples, dtype=np.int16).tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def _tts(handler, **kwargs) -> SarvamTTS:
    kwargs.setdefault("streaming", False)   # REST-path tests never open a socket
    tts = SarvamTTS(api_key="sk-test", **kwargs)
    tts._client = httpx.AsyncClient(base_url="https://api.sarvam.ai", transport=httpx.MockTransport(handler),
                                    headers={"api-subscription-key": "sk-test"})
    return tts


@pytest.mark.asyncio
async def test_request_shape_and_pcm_passthrough():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers["api-subscription-key"]
        return httpx.Response(200, json={"request_id": "r1", "audios": [_wav(16000)]})

    pcm = await _tts(handler, speaker="simran_enhi_customer").synthesize("नमस्ते, how are you?", 16000, language="hi")
    assert seen["key"] == "sk-test"
    assert seen["body"] == {
        "text": "नमस्ते, how are you?", "language_code": "hi-IN", "speaker": "simran_enhi_customer",
        "model": "bulbul:v4-flash",
        "speech_sample_rate": 16000, "output_audio_codec": "wav",
    }
    assert pcm == np.arange(160, dtype=np.int16).tobytes()


@pytest.mark.asyncio
async def test_english_and_unknown_languages():
    codes = []

    def handler(request: httpx.Request) -> httpx.Response:
        codes.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    await tts.synthesize("Hello", 16000, language="en-US")
    await tts.synthesize("Hola", 16000, language="es")     # not Sarvam's: Hindi voice default
    await tts.synthesize("Hi", 16000)                      # no instance language: Hindi default
    assert codes == ["en-IN", "hi-IN", "hi-IN"]


@pytest.mark.asyncio
async def test_unserved_rate_is_resampled():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["speech_sample_rate"] == 16000
        return httpx.Response(200, json={"audios": [_wav(16000, samples=1600)]})

    pcm = await _tts(handler).synthesize("hi", 12000)
    assert abs(len(pcm) // 2 - 1200) <= 2


@pytest.mark.asyncio
async def test_errors_return_silence_not_exceptions():
    tts = _tts(lambda r: httpx.Response(429, json={"error": {"code": "insufficient_quota_error"}}))
    assert await tts.synthesize("hi", 16000) == b""
    tts = _tts(lambda r: httpx.Response(200, json={"audios": ["not-base64-wav"]}))
    assert await tts.synthesize("hi", 16000) == b""


def test_multilingual_agents_may_use_sarvam_for_indian_languages_and_english():
    assert tts_languages("sarvam", "bulbul:v3") == frozenset({"en", "hi", "ta", "te", "kn", "mr", "bn"})
    assert tts_languages("sarvam", "bulbul:v4-flash", "shubh_enhi_ads") == frozenset({"en", "hi"})


@pytest.mark.asyncio
async def test_short_phrases_are_cached_per_language_and_rate():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    first = await tts.synthesize("One moment.", 16000, language="en")
    assert await tts.synthesize("One moment.", 16000, language="en") == first
    await tts.synthesize("One moment.", 16000, language="hi")
    assert calls == ["en-IN", "hi-IN"]


@pytest.mark.asyncio
async def test_prewarm_fills_cache_once():
    from services.conversation.i18n import first_turn_fillers, tool_fillers

    texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    await tts.prewarm(["en", "hi"])
    await tts.prewarm(["en", "hi"])
    expected = {p for lang in ("en", "hi") for p, _ in (*first_turn_fillers(lang), *tool_fillers(lang))}
    assert sorted(texts) == sorted(expected)
    before = len(texts)
    await tts.synthesize(tool_fillers("hi")[0][0], 16000, language="hi")
    assert len(texts) == before


@pytest.mark.asyncio
async def test_pinned_phrases_survive_lru_churn(monkeypatch):
    import services.conversation.providers.tts.sarvam as sarvam

    monkeypatch.setattr(sarvam, "_CACHE_ENTRIES", 3)
    texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    await tts.prewarm(["en"])
    greeting = "Namaste, thank you for calling Medivista Multispeciality Hospital. " * 3   # > 120 chars
    await tts.pin_phrases({"en": [greeting]})
    for i in range(10):                                  # one-off sentences churn the LRU
        await tts.synthesize(f"Sentence number {i}.", 16000, language="en")
    before = len(texts)
    await tts.synthesize("One moment.", 16000, language="en")
    await tts.synthesize(greeting, 16000, language="en")
    await tts.pin_phrases({"en": [greeting]})            # already pinned: no request
    assert len(texts) == before


@pytest.mark.asyncio
async def test_pin_without_language_matches_synthesize_without_language():
    texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler, language="en-IN")
    await tts.pin_phrases({None: ["Welcome to the clinic."]}, 16000)
    await tts.synthesize("Welcome to the clinic.", 16000)
    assert texts == ["en-IN"]


# ── WebSocket streaming ──

class _FakeSarvamWs:
    """Answers each text+flush with the scripted PCM chunks, then the 'final' event."""

    def __init__(self, chunks_per_sentence: list[list[bytes]], *, fail_after_config: bool = False):
        self.sent: list[dict] = []
        self.closed = False
        self._scripts = list(chunks_per_sentence)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._fail = fail_after_config

    async def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if msg["type"] == "flush":
            if self._fail:
                await self._queue.put({"type": "error", "data": {"message": "bad speaker"}})
                return
            for chunk in self._scripts.pop(0):
                await self._queue.put({"type": "audio", "data": {"content_type": "audio/pcm",
                                                                  "audio": base64.b64encode(chunk).decode()}})
            await self._queue.put({"type": "event", "data": {"event_type": "final"}})

    async def recv(self):
        return json.dumps(await self._queue.get())

    async def close(self):
        self.closed = True


def _streaming_tts(ws_list, rest_handler=None):
    tts = _tts(rest_handler or (lambda r: httpx.Response(500)), streaming=True)
    connects = []

    async def _connect(url, **kwargs):
        assert url.startswith("wss://api.sarvam.ai/text-to-speech/ws?model=bulbul")
        assert kwargs["additional_headers"]["Api-Subscription-Key"] == "sk-test"
        connects.append(url)
        return ws_list.pop(0)

    tts._ws_connect = _connect
    return tts, connects


@pytest.mark.asyncio
async def test_streams_chunks_and_reuses_the_connection():
    ws = _FakeSarvamWs([[b"\x01\x00\x02", b"\x00\x03\x00"], [b"\x04\x00"]])   # odd split across chunks
    tts, connects = _streaming_tts([ws])
    first = [c async for c in tts.synthesize_stream("Pehla vaakya, kuch lamba sa.", 16000, language="hi")]
    second = [c async for c in tts.synthesize_stream("Doosra.", 16000, language="hi")]
    assert first == [b"\x01\x00", b"\x02\x00\x03\x00"]          # whole 16-bit samples only
    assert second == [b"\x04\x00"]
    assert len(connects) == 1 and not ws.closed                  # pooled, one config
    assert [m["type"] for m in ws.sent] == ["config", "text", "flush", "text", "flush"]
    assert ws.sent[0]["data"]["language_code"] == "hi-IN" and ws.sent[0]["data"]["output_audio_codec"] == "linear16"


@pytest.mark.asyncio
async def test_barge_in_closes_the_connection_instead_of_reusing_it():
    ws1 = _FakeSarvamWs([[b"\x01\x00", b"\x02\x00", b"\x03\x00"]])
    ws2 = _FakeSarvamWs([[b"\x09\x00"]])
    tts, connects = _streaming_tts([ws1, ws2])
    stream = tts.synthesize_stream("Interrupted sentence here.", 16000, language="en")
    assert await stream.__anext__() == b"\x01\x00"
    await stream.aclose()                                        # caller barged in
    assert ws1.closed
    assert [c async for c in tts.synthesize_stream("Next one.", 16000, language="en")] == [b"\x09\x00"]
    assert len(connects) == 2


@pytest.mark.asyncio
async def test_stream_error_before_audio_falls_back_to_rest():
    ws = _FakeSarvamWs([[]], fail_after_config=True)
    tts, _ = _streaming_tts([ws], rest_handler=lambda r: httpx.Response(200, json={"audios": [_wav(16000, 4)]}))
    out = [c async for c in tts.synthesize_stream("Fallback please.", 16000, language="en")]
    assert out == [np.arange(4, dtype=np.int16).tobytes()] and ws.closed


@pytest.mark.asyncio
async def test_pinned_phrases_skip_the_socket():
    tts, connects = _streaming_tts([], rest_handler=lambda r: httpx.Response(200, json={"audios": [_wav(16000)]}))
    await tts.pin_phrases({"en": ["Welcome to the clinic."]}, 16000)
    out = [c async for c in tts.synthesize_stream("Welcome to the clinic.", 16000, language="en")]
    assert out and connects == []


@pytest.mark.asyncio
async def test_idle_connections_are_retired(monkeypatch):
    import services.conversation.providers.tts.sarvam as sarvam

    ws1, ws2 = _FakeSarvamWs([[b"\x01\x00"]]), _FakeSarvamWs([[b"\x02\x00"]])
    tts, connects = _streaming_tts([ws1, ws2])
    [c async for c in tts.synthesize_stream("One.", 16000, language="en")]
    clock = {"t": sarvam.time.monotonic() + sarvam._WS_IDLE_MAX_S + 1}
    monkeypatch.setattr(sarvam.time, "monotonic", lambda: clock["t"])
    [c async for c in tts.synthesize_stream("Two.", 16000, language="en")]
    assert ws1.closed and len(connects) == 2


@pytest.mark.asyncio
async def test_prewarm_opens_one_socket_per_language_once():
    ws_hi, ws_en = _FakeSarvamWs([[b"\x01\x00"]]), _FakeSarvamWs([])
    tts, connects = _streaming_tts([ws_hi, ws_en],
                                   rest_handler=lambda r: httpx.Response(200, json={"audios": [_wav(16000)]}))
    await tts.prewarm(["hi", "en"])
    await tts.prewarm(["hi", "en"])                              # still warm: no new sockets
    assert len(connects) == 2
    assert [c async for c in tts.synthesize_stream("Pehli baat.", 16000, language="hi")] == [b"\x01\x00"]
    assert len(connects) == 2                                    # first reply used the warm socket
