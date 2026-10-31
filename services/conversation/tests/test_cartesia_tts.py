"""CartesiaTTS request body: speed is opt-in and shaped per model generation."""

from __future__ import annotations

import pytest

from services.conversation.ai_provider_manager import ProviderConfig, _make_cartesia_tts
from services.conversation.providers.tts.cartesia import CartesiaTTS

_BASE = {
    "model_id": "sonic-2",
    "transcript": "hi",
    "voice": {"mode": "id", "id": "v"},
    "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
}


def _body(**kw) -> dict:
    return CartesiaTTS(api_key="k", voice="v", **kw)._body("hi", 16000)


def test_no_speed_leaves_body_unchanged():
    assert _body() == _BASE


def test_sonic3_uses_generation_config():
    assert _body(model="sonic-3", speed=0.85)["generation_config"] == {"speed": 0.85}
    assert "__experimental_controls" not in _body(model="sonic-3", speed=0.85)


def test_sonic2_uses_experimental_controls():
    body = _body(speed=0.75)
    assert body["__experimental_controls"] == {"speed": -0.5}
    assert "generation_config" not in body


def test_speed_is_clamped():
    assert _body(model="sonic-3", speed=9)["generation_config"]["speed"] == 1.5
    assert _body(model="sonic-3", speed=0.1)["generation_config"]["speed"] == 0.6
    assert _body(speed=0.1)["__experimental_controls"]["speed"] == pytest.approx(-0.8)


def _cfg(extra: dict) -> ProviderConfig:
    return ProviderConfig(id="c", role="tts", engine="cartesia", voice="v", extra=extra)


async def test_factory_passes_extra_speed():
    tts = await _make_cartesia_tts(_cfg({"model": "sonic-3", "speed": "0.85"}), "key")
    assert tts._body("hi", 16000)["generation_config"] == {"speed": 0.85}


@pytest.mark.parametrize("bad", ["fast", True, float("nan")])
async def test_factory_rejects_non_numeric_speed(bad):
    with pytest.raises(ValueError, match="extra.speed"):
        await _make_cartesia_tts(_cfg({"speed": bad}), "key")


# ── Language ─────────────────────────────────────────────────────────────────

def test_body_includes_instance_language():
    assert CartesiaTTS(api_key="k", voice="v", language="hi")._body("hi", 16000)["language"] == "hi"


def test_body_per_call_language_overrides_instance():
    tts = CartesiaTTS(api_key="k", voice="v", language="en")
    assert tts._body("नमस्ते", 16000, "hi")["language"] == "hi"


def test_body_without_language_is_unchanged():
    assert "language" not in _body()


async def test_factory_passes_row_language():
    cfg = ProviderConfig(id="c", role="tts", engine="cartesia", voice="v", language="hi", extra={})
    tts = await _make_cartesia_tts(cfg, "key")
    assert tts._body("hi", 16000)["language"] == "hi"


async def test_synthesize_sends_per_call_language():
    import json as _json

    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_json.loads(request.content))
        return httpx.Response(200, content=b"\x00\x00")

    tts = CartesiaTTS(api_key="k", voice="v")
    tts._client = httpx.AsyncClient(base_url="https://api.cartesia.ai", transport=httpx.MockTransport(handler))
    await tts.synthesize("नमस्ते", 16000, language="hi")
    assert seen["language"] == "hi"


def test_body_language_is_normalised_to_iso_639_1():
    assert CartesiaTTS(api_key="k", voice="v", language="en-US")._body("hi", 16000)["language"] == "en"
    assert CartesiaTTS(api_key="k", voice="v")._body("hi", 16000, "hi-IN")["language"] == "hi"


async def test_stream_reports_word_end_bytes_from_timestamp_events():
    import base64
    import json as _json

    import httpx

    seen = {}

    def event(**kw) -> str:
        return f"data: {_json.dumps(kw)}\n\n"

    audio = base64.b64encode(b"\x01\x00" * 8000).decode()
    sse = (
        event(type="chunk", data=audio)
        + event(type="timestamps", word_timestamps={"words": ["Hello", "there."], "start": [0.0, 0.2], "end": [0.2, 0.5]})
        + event(type="done")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_json.loads(request.content))
        return httpx.Response(200, content=sse.encode())

    tts = CartesiaTTS(api_key="k", voice="v")
    tts._client = httpx.AsyncClient(base_url="https://api.cartesia.ai", transport=httpx.MockTransport(handler))
    chunks = [c async for c in tts.synthesize_stream("Hello there.", 16000)]

    assert seen["add_timestamps"] is True
    assert chunks == [b"\x01\x00" * 8000]
    # 16 kHz, 2 bytes per sample: 0.2 s and 0.5 s.
    assert chunks[0].word_ends == [6400, 16000]


async def test_stream_ignores_a_malformed_timestamps_event_and_keeps_the_audio():
    import base64
    import json as _json

    import httpx

    def event(**kw) -> str:
        return f"data: {_json.dumps(kw)}\n\n"

    audio = base64.b64encode(b"\x01\x00" * 8).decode()
    sse = (
        event(type="chunk", data=audio)
        + event(type="timestamps", word_timestamps={"words": ["a"]})            # no "end"
        + event(type="timestamps", word_timestamps={"end": [None]})             # null entry
        + event(type="timestamps")                                             # no word_timestamps
        + event(type="chunk", data=audio)
        + event(type="done")
    )
    tts = CartesiaTTS(api_key="k", voice="v")
    tts._client = httpx.AsyncClient(
        base_url="https://api.cartesia.ai",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=sse.encode())),
    )

    chunks = [c async for c in tts.synthesize_stream("a b", 16000)]

    assert chunks == [b"\x01\x00" * 8, b"\x01\x00" * 8]
