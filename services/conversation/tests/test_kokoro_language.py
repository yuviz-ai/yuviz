"""KokoroTTS per-language pipelines, with a stub `kokoro` module (no weights)."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest


class _StubPipeline:
    built: list[tuple[str, object]] = []

    def __init__(self, lang_code, model=True):
        self.lang_code = lang_code
        self.model = model if model is not True else object()
        _StubPipeline.built.append((lang_code, model))

    def __call__(self, text, voice, speed):
        yield text, None, np.zeros(240, dtype=np.float32)


@pytest.fixture
def kokoro(monkeypatch):
    _StubPipeline.built = []
    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=_StubPipeline))
    from services.conversation.providers.tts import kokoro as mod
    return mod


def test_iso_to_kokoro_lang_code(kokoro):
    assert kokoro.kokoro_lang_code("en") == "a"
    assert kokoro.kokoro_lang_code("hi") == "h"
    assert kokoro.kokoro_lang_code("hi-IN") == "h"
    expected = {"es": "e", "fr": "f", "it": "i", "pt": "p"}
    assert {k: kokoro.kokoro_lang_code(k) for k in expected} == expected
    # ja/zh need G2P extras that aren't installed: no Kokoro pipeline for them.
    for code in ("de", "ja", "zh"):
        assert kokoro.kokoro_lang_code(code) is None
    assert kokoro.kokoro_lang_code(None) is None


async def test_pipelines_are_lazy_cached_and_share_the_model(kokoro):
    tts = kokoro.KokoroTTS(voice="hf_alpha", lang_code="a")
    assert [code for code, _ in _StubPipeline.built] == ["a"]
    base_model = tts._pipelines["a"].model

    await tts.synthesize("नमस्ते", 16000, language="hi")
    await tts.synthesize("फिर से", 16000, language="hi")
    await tts.synthesize("hello", 16000)

    assert [code for code, _ in _StubPipeline.built] == ["a", "h"]
    assert _StubPipeline.built[1][1] is base_model


async def test_unknown_language_falls_back_to_row_pipeline(kokoro):
    tts = kokoro.KokoroTTS(lang_code="a")
    audio = await tts.synthesize("Hallo", 16000, language="de")
    assert audio
    assert [code for code, _ in _StubPipeline.built] == ["a"]


async def test_prewarm_builds_pipelines_up_front(kokoro):
    tts = kokoro.KokoroTTS(voice="custom", lang_code="a")  # no language prefix
    await tts.prewarm(["en", "hi", "es"])
    assert sorted(code for code, _ in _StubPipeline.built) == ["a", "e", "h"]


async def test_failed_pipeline_build_falls_back_once_and_is_not_retried(kokoro, monkeypatch, caplog):
    tts = kokoro.KokoroTTS(voice="hf_alpha", lang_code="a")
    real = _StubPipeline.__init__

    def failing(self, lang_code, model=True):
        if lang_code == "h":
            raise ModuleNotFoundError("No module named 'some_g2p_extra'")
        real(self, lang_code, model)

    monkeypatch.setattr(_StubPipeline, "__init__", failing)
    first = await tts.synthesize("नमस्ते", 16000, language="hi")
    second = await tts.synthesize("फिर से", 16000, language="hi")
    assert first and second  # spoken with the row's pipeline, not silence
    assert sum("can't build pipeline" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("language", ["en", "en-GB", "en-US"])
async def test_same_language_as_row_keeps_the_rows_own_accent(kokoro, language):
    tts = kokoro.KokoroTTS(lang_code="b")
    assert tts._resolve_lang_code(language) == "b"
    await tts.synthesize("hello", 16000, language=language)
    assert [code for code, _ in _StubPipeline.built] == ["b"]


async def test_voice_never_switches_to_another_languages_pipeline(kokoro):
    # A single-language agent set to en-IN on a Hindi voice keeps the row's pipeline, as before
    # agents.language reached Kokoro; an English voice never builds a Hindi pipeline.
    hindi = kokoro.KokoroTTS(voice="hf_alpha", lang_code="h")
    await hindi.synthesize("hello", 16000, language="en-IN")
    english = kokoro.KokoroTTS(voice="af_heart", lang_code="a")
    await english.prewarm(["en", "hi"])
    await english.synthesize("नमस्ते", 16000, language="hi")
    assert [code for code, _ in _StubPipeline.built] == ["h", "a"]
