"""Console voice typing: the clip goes to the agent's own hearing model; no database or network."""

from __future__ import annotations

import httpx
import pytest

from services.config import voice_input

_RealAsyncClient = httpx.AsyncClient
TENANT = {"id": "t1", "default_stt_config_id": None}


class _Secrets:
    async def resolve(self, ref):
        return "key-" + ref


def _install(monkeypatch, cfg, handler):
    async def get(_id):
        return cfg
    monkeypatch.setattr(voice_input, "get_provider_config", get)
    monkeypatch.setattr(voice_input.httpx, "AsyncClient",
                        lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler), **kw))


def _cfg(engine, model, **extra):
    return {"id": "p1", "tenant_id": "t1", "role": "stt", "engine": engine, "model": model,
            "api_key_ref": "env:K", "extra": extra}


async def test_deepgram_hears_with_the_agents_model_and_code_switches_for_two_languages(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(params=dict(request.url.params), ctype=request.headers["content-type"], auth=request.headers["authorization"])
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": " make it warmer "}]}]}})

    _install(monkeypatch, _cfg("deepgram", "nova-3"), handler)
    agent = {"stt_config_id": "p1", "language": "en", "supported_languages": ["en", "hi"]}
    result = await voice_input.transcribe_clip(TENANT, agent, b"opus", "audio/webm;codecs=opus", secret_resolver=_Secrets())
    assert (result["text"], result["fallback"]) == ("make it warmer", False)
    assert seen["params"]["model"] == "nova-3" and seen["params"]["language"] == "multi"
    assert seen["ctype"] == "audio/webm" and seen["auth"] == "Token key-env:K"


async def test_sarvam_auto_detects_for_two_languages_and_keeps_the_v3_output_mode(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"transcript": "थोड़ा विनम्र बनाओ"})

    _install(monkeypatch, _cfg("sarvam", "saaras:v3", mode="codemix"), handler)
    agent = {"stt_config_id": "p1", "language": "hi", "supported_languages": ["hi", "en"]}
    result = await voice_input.transcribe_clip(TENANT, agent, b"m4a", "audio/mp4", secret_resolver=_Secrets())
    assert result["text"] == "थोड़ा विनम्र बनाओ"
    assert b'name="language_code"\r\n\r\nunknown' in seen["body"] and b'name="mode"\r\n\r\ncodemix' in seen["body"]
    assert b'filename="clip.mp4"' in seen["body"]


async def test_a_local_or_missing_hearing_model_borrows_the_platforms_cloud_pick(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    for key in ("SARVAM_API_KEY",):
        monkeypatch.delenv(key, raising=False)
    seen = {}

    def handler(request):
        seen["model"] = request.url.params["model"]
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": "hello"}]}]}})

    for agent, cfg in (({"stt_config_id": "p1", "language": "en"}, _cfg("faster_whisper", "small")),
                       ({"stt_config_id": None, "language": "en"}, None)):
        _install(monkeypatch, cfg, handler)
        result = await voice_input.transcribe_clip(TENANT, agent, b"x", "audio/webm", secret_resolver=_Secrets())
        assert result == {"text": "hello", "heard_by": "Deepgram Nova-3", "fallback": True}
        assert seen["model"] == "nova-3"


async def test_no_cloud_hearing_on_the_platform_says_so(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "SARVAM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    _install(monkeypatch, _cfg("faster_whisper", "small"), lambda r: httpx.Response(500))
    with pytest.raises(voice_input.VoiceInputUnavailable, match="none is set up on this platform"):
        await voice_input.transcribe_clip(TENANT, {"stt_config_id": "p1"}, b"x", "audio/webm", secret_resolver=_Secrets())


@pytest.mark.parametrize("cfg, ctype, audio, message", [
    (_cfg("deepgram", "nova-3"), "text/html", b"x", "unsupported recording format"),
    (_cfg("deepgram", "nova-3"), "audio/webm", b"x" * (voice_input.MAX_CLIP_BYTES + 1), "too long"),
    (_cfg("deepgram", "nova-3"), "audio/webm", b"", "empty"),
])
async def test_unusable_clips_say_how_to_fix_it(monkeypatch, cfg, ctype, audio, message):
    _install(monkeypatch, cfg, lambda r: httpx.Response(500))
    with pytest.raises(voice_input.VoiceInputUnavailable, match=message):
        await voice_input.transcribe_clip(TENANT, {"stt_config_id": "p1"}, audio, ctype, secret_resolver=_Secrets())
