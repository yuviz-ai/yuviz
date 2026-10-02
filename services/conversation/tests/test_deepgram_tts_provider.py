"""DeepgramTTS tests via httpx.MockTransport; synthesize_stream() must forward chunks as they arrive."""

from __future__ import annotations

import httpx

from services.conversation.providers.tts.deepgram import DeepgramTTS


def _make_provider(handler) -> DeepgramTTS:
    provider = DeepgramTTS(api_key="test-key", voice="aura-asteria-en")
    provider._client = httpx.AsyncClient(base_url="https://api.deepgram.com", transport=httpx.MockTransport(handler))
    return provider


async def test_synthesize_returns_full_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["model"] == "aura-asteria-en"
        assert request.url.params["encoding"] == "linear16"
        assert request.url.params["sample_rate"] == "16000"
        return httpx.Response(200, content=b"\x01\x02" * 100)

    provider = _make_provider(handler)
    audio = await provider.synthesize("Hello there.", 16_000)

    assert audio == b"\x01\x02" * 100


async def test_synthesize_empty_text_short_circuits_without_a_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never be called for empty text")

    provider = _make_provider(handler)
    assert await provider.synthesize("   ", 16_000) == b""


async def test_synthesize_stream_forwards_each_chunk_not_the_buffered_whole():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"\x01\x02" * 100))

    provider = _make_provider(handler)
    chunks = [c async for c in provider.synthesize_stream("Hello there.", 16_000)]

    assert b"".join(chunks) == b"\x01\x02" * 100
    assert all(chunks)  # no empty chunks forwarded


async def test_synthesize_stream_empty_text_yields_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never be called for empty text")

    provider = _make_provider(handler)
    chunks = [c async for c in provider.synthesize_stream("", 16_000)]

    assert chunks == []


async def test_synthesize_stream_http_error_yields_nothing_not_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"internal error")

    provider = _make_provider(handler)
    chunks = [c async for c in provider.synthesize_stream("Hello.", 16_000)]

    assert chunks == []


class _RawByteChunks(httpx.AsyncByteStream):
    """Async-iterable of exact byte chunks, bypassing httpx's own buffering —
    simulates real HTTP chunk boundaries landing mid-sample."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


async def test_synthesize_stream_realigns_odd_length_chunk_boundaries():
    # 5- then 3-byte chunks: each yielded chunk must be sample-aligned, bytes unchanged.
    full = b"\x01\x02\x03\x04\x05\x06\x07\x08"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_RawByteChunks([full[:5], full[5:]]))

    provider = _make_provider(handler)
    chunks = [c async for c in provider.synthesize_stream("Hello there.", 16_000)]

    assert all(len(c) % 2 == 0 for c in chunks)
    assert b"".join(chunks) == full
