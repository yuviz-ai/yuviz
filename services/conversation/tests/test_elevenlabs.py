"""ElevenLabsTTS tests via httpx.MockTransport — no network, no cost."""

from __future__ import annotations

import numpy as np
import pytest

import httpx

from services.conversation.providers.tts.elevenlabs import ElevenLabsTTS, _nearest_supported_rate


def _make_tts(handler, **kwargs) -> ElevenLabsTTS:
    tts = ElevenLabsTTS(api_key="test-key", voice_id="voice-123", **kwargs)
    tts._client = httpx.AsyncClient(
        base_url="https://api.elevenlabs.io",
        headers={"xi-api-key": "test-key"},
        transport=httpx.MockTransport(handler),
    )
    return tts


def _silence_pcm(n_samples: int) -> bytes:
    return np.zeros(n_samples, dtype=np.int16).tobytes()


@pytest.mark.parametrize("requested,expected", [
    (16000, 16000),   # exact match already supported
    (8000, 8000),
    (24000, 24000),
    (12000, 16000),   # rounds up to nearest supported rate
    (44100, 44100),
    (48000, 44100),   # above the max supported rate — clamp to the largest
])
def test_nearest_supported_rate(requested, expected):
    assert _nearest_supported_rate(requested) == expected


async def test_synthesize_empty_text_short_circuits_without_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not make a request for empty text")

    tts = _make_tts(handler)
    assert await tts.synthesize("   ", 16000) == b""


async def test_synthesize_returns_pcm_unchanged_when_rate_already_supported():
    pcm = _silence_pcm(1600)  # exactly 100ms @ 16kHz

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "test-key"
        assert request.url.params["output_format"] == "pcm_16000"
        return httpx.Response(200, content=pcm)

    tts = _make_tts(handler)
    result = await tts.synthesize("hello", 16000)

    assert result == pcm


async def test_synthesize_resamples_when_rate_unsupported():
    # 12 kHz is unsupported: request 16 kHz and resample down.
    pcm_at_16k = _silence_pcm(1600)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["output_format"] == "pcm_16000"
        return httpx.Response(200, content=pcm_at_16k)

    tts = _make_tts(handler)
    result = await tts.synthesize("hello", 12000)

    # Resampled length should scale roughly by 12000/16000, not equal the input.
    assert len(result) != len(pcm_at_16k)
    expected_samples = round(1600 * 12000 / 16000)
    actual_samples = len(result) // 2  # int16 = 2 bytes/sample
    assert abs(actual_samples - expected_samples) <= 1


async def test_synthesize_returns_empty_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    tts = _make_tts(handler)
    result = await tts.synthesize("hello", 16000)

    assert result == b""


async def test_synthesize_sends_language_code_when_set():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body["language_code"] == "hi"
        return httpx.Response(200, content=_silence_pcm(1600))

    tts = _make_tts(handler, language_code="hi")
    await tts.synthesize("hello", 16000)


async def test_synthesize_omits_language_code_when_unset():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert "language_code" not in body
        return httpx.Response(200, content=_silence_pcm(1600))

    tts = _make_tts(handler)
    await tts.synthesize("hello", 16000)


async def test_synthesize_per_call_language_overrides_row():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        assert _json.loads(request.content)["language_code"] == "hi"
        return httpx.Response(200, content=_silence_pcm(1600))

    tts = _make_tts(handler, language_code="en")
    chunks = [c async for c in tts.synthesize_stream("नमस्ते", 16000, language="hi")]
    assert chunks


async def test_regional_language_code_is_normalised():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        assert _json.loads(request.content)["language_code"] == "en"
        return httpx.Response(200, content=_silence_pcm(1600))

    tts = _make_tts(handler, language_code="en-US")
    await tts.synthesize("hello", 16000)


@pytest.mark.parametrize("model_id", ["eleven_multilingual_v2", "eleven_turbo_v2", "eleven_flash_v2", "eleven_monolingual_v1"])
async def test_models_without_language_enforcement_never_get_language_code(model_id):
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        assert "language_code" not in _json.loads(request.content)
        return httpx.Response(200, content=_silence_pcm(1600))

    tts = _make_tts(handler, model_id=model_id, language_code="en-US")
    await tts.synthesize("hello", 16000)
    await tts.synthesize("नमस्ते", 16000, language="hi")


async def test_flash_v2_5_sends_normalised_language_code():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        assert _json.loads(request.content)["language_code"] == "en"
        return httpx.Response(200, content=_silence_pcm(1600))

    tts = _make_tts(handler, model_id="eleven_flash_v2_5", language_code="en-US")
    await tts.synthesize("hello", 16000)
