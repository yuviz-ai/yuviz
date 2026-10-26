"""FasterWhisperSTT language detection, with a stub model (no weights loaded)."""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def whisper_cls(monkeypatch):
    # The real package is optional in test envs; the constructor only imports the name.
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=object))
    from services.conversation.providers.stt.faster_whisper import FasterWhisperSTT
    return FasterWhisperSTT


class _StubModel:
    def __init__(self, language="hi", probability=0.93, multilingual=True):
        self.model = SimpleNamespace(is_multilingual=multilingual)  # the ctranslate2 model
        self.calls: list[dict] = []
        self._info = SimpleNamespace(language=language, language_probability=probability)

    def transcribe(self, pcm, **kwargs):
        self.calls.append(kwargs)
        seg = SimpleNamespace(text=" haan ji ", no_speech_prob=0.1, avg_logprob=-0.2)
        return [seg], self._info


def _stt(cls, language, model_size="small", model=None):
    stt = cls(model_size=model_size, language=language)
    stt._model = model or _StubModel()
    return stt


async def test_auto_detect_reports_language_and_probability(whisper_cls):
    stt = _stt(whisper_cls, language=None)
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000)
    assert result.text == "haan ji"
    assert result.language == "hi"
    assert result.language_confidence == pytest.approx(0.93)
    assert stt._model.calls[0]["language"] is None


async def test_forced_language_reports_no_detection(whisper_cls):
    stt = _stt(whisper_cls, language="en")
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000)
    assert result.language is None
    assert stt._model.calls[0]["language"] == "en"


async def test_per_call_language_overrides_instance(whisper_cls):
    stt = _stt(whisper_cls, language="en")
    result = await stt.finalize_stream("s1", b"\x00\x01" * 8000, 16000, language=None)
    assert stt._model.calls[0]["language"] is None
    assert result.language == "hi"


async def test_english_only_model_logs_once_when_asked_to_detect(whisper_cls, caplog):
    stt = _stt(whisper_cls, language=None, model_size="small.en")
    await stt.transcribe(b"\x00\x01" * 8000, 16000)
    await stt.transcribe(b"\x00\x01" * 8000, 16000)
    assert sum("English-only" in r.message for r in caplog.records) == 1


class _DetectingModel(_StubModel):
    """Whisper hearing Hindi as Urdu: ur tops the probabilities, hi is close behind."""

    def detect_language(self, pcm):
        probs = [("ur", 0.55), ("hi", 0.35), ("en", 0.05), ("es", 0.05)]
        return probs[0][0], probs[0][1], probs


async def test_candidates_fold_urdu_into_hindi_and_decode_in_hindi(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_DetectingModel())
    result = await stt.finalize_stream("s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"))
    assert stt._model.calls[0]["language"] == "hi"  # one decode, in Hindi (Devanagari), not Urdu
    assert result.language == "hi"
    # (ur 0.55 + hi 0.35) / (that + en 0.05): share among the agent's languages.
    assert result.language_confidence == pytest.approx(0.90 / 0.95)


async def test_unsure_outside_language_keeps_the_agent_language(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_DetectingModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, language=None, languages=("en", "es"))
    # Whisper's top pick (ur, 0.55) is neither agent language but not confident: an accented
    # caller must not be reported as speaking a third language.
    assert result.language in ("en", "es")
    assert stt._model.calls[0]["language"] in ("en", "es")


class _ConfidentFrenchModel(_StubModel):
    """Clear French speech to an en+es agent."""

    def detect_language(self, pcm):
        probs = [("fr", 0.92), ("en", 0.04), ("es", 0.04)]
        return probs[0][0], probs[0][1], probs


async def test_confident_unsupported_language_is_reported_as_that_language(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_ConfidentFrenchModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, language=None, languages=("en", "es"))
    # The tracker is told French at Whisper's own probability; the text still decodes in an agent language.
    assert result.language == "fr" and result.language_confidence == pytest.approx(0.92)
    assert stt._model.calls[0]["language"] in ("en", "es")


class _AccentedOkayModel(_StubModel):
    """Accented "okay": Whisper is confidently wrong (a third language) with real mass on English."""

    def detect_language(self, pcm):
        probs = [("nn", 0.75), ("en", 0.24), ("hi", 0.01)]
        return probs[0][0], probs[0][1], probs


@pytest.mark.parametrize("require_language", [None, ("en", 0.0)])
async def test_accented_agent_language_speech_stays_in_the_agent_language(whisper_cls, require_language):
    stt = _stt(whisper_cls, language=None, model=_AccentedOkayModel())
    result = await stt.transcribe(
        b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"), require_language=require_language,
    )
    assert result.language == "en"
    assert result.language_confidence == pytest.approx(0.24 / 0.25)


async def test_a_hindi_session_switches_back_on_the_accented_english_utterance(whisper_cls):
    from services.conversation.language import LanguageTracker, utterance_language

    stt = _stt(whisper_cls, language=None, model=_AccentedOkayModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"))
    tracker = LanguageTracker("en", ("en", "hi"), unsupported_to_default=True)
    tracker.current, tracker._has_evidence = "hi", True
    assert tracker.observe(utterance_language(result, ("en", "hi"))) is True
    assert tracker.current == "en"


async def test_a_confident_third_language_is_not_reported_for_a_short_utterance(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_ConfidentFrenchModel())
    result = await stt.transcribe(
        b"\x00\x01" * 8000, 16000, language=None, languages=("en", "es"), require_language=("en", 0.0),
    )
    assert result.language == "en"


async def test_fixed_language_ignores_candidates(whisper_cls):
    stt = _stt(whisper_cls, language="en", model=_DetectingModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, languages=("en", "hi"))
    assert stt._model.calls[0]["language"] == "en" and result.language is None


class _AccentedEnglishModel(_StubModel):
    """Accented English: Whisper's top pick is English, but most mass is spread elsewhere."""

    def detect_language(self, pcm):
        probs = [("en", 0.48), ("cy", 0.2), ("nl", 0.15), ("hi", 0.06), ("ur", 0.04), ("de", 0.07)]
        return probs[0][0], probs[0][1], probs


async def test_accented_english_is_confidently_english_among_candidates(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_AccentedEnglishModel())
    result = await stt.transcribe(b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"))
    assert result.language == "en"
    assert result.language_confidence == pytest.approx(0.48 / 0.58)  # 0.83: enough to switch back


# ── Review fixes: English-only models, regional tags, short-blip early reject ──

class _EnglishOnlyModel(_DetectingModel):
    def __init__(self):
        super().__init__(multilingual=False)

    def detect_language(self, pcm):
        raise RuntimeError("detect_language can only be called on multilingual models")


async def test_english_only_model_skips_detection_instead_of_crashing(whisper_cls):
    stt = _stt(whisper_cls, language=None, model_size="small.en", model=_EnglishOnlyModel())
    result = await stt.finalize_stream("s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"))
    assert result.text == "haan ji"
    assert stt._model.calls[0]["language"] is None  # faster-whisper decodes English itself


async def test_regional_tag_is_normalised_for_whisper(whisper_cls):
    stt = _stt(whisper_cls, language="en-US")
    await stt.transcribe(b"\x00\x01" * 8000, 16000)
    await stt.transcribe(b"\x00\x01" * 8000, 16000, language="hi-IN")
    assert [c["language"] for c in stt._model.calls] == ["en", "hi"]


async def test_short_blip_in_other_language_skips_the_decode(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_DetectingModel())  # detects hi (0.95 of en+hi)
    result = await stt.finalize_stream(
        "s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"),
        require_language=("en", 0.8),
    )
    assert result.text == "" and result.language == "hi"
    assert stt._model.calls == []  # no transcribe/decode at all


async def test_short_utterance_in_session_language_is_decoded(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_DetectingModel())
    result = await stt.finalize_stream(
        "s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"),
        require_language=("hi", 0.8),
    )
    assert result.text == "haan ji" and stt._model.calls[0]["language"] == "hi"


class _NoSpeechModel(_DetectingModel):
    """A cough Whisper decodes to text but flags as probably-not-speech, with a fair logprob."""

    def transcribe(self, pcm, **kwargs):
        self.calls.append(kwargs)
        seg = SimpleNamespace(text=" ji ", no_speech_prob=0.7, avg_logprob=-0.3)
        return [seg], self._info


async def test_no_speech_prob_alone_drops_a_short_segment_but_not_a_full_one(whisper_cls):
    stt = _stt(whisper_cls, language=None, model=_NoSpeechModel())
    short = await stt.finalize_stream(
        "s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"),
        require_language=("hi", 0.0),
    )
    assert short.text == ""
    full = await stt.finalize_stream("s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"))
    assert full.text == "ji"  # full-length path still needs the low logprob too


class _UnsureHindiModel(_StubModel):
    """A short Hindi "haan": Hindi tops the agent's languages, but not confidently."""

    def detect_language(self, pcm):
        probs = [("hi", 0.30), ("en", 0.25), ("de", 0.2), ("cy", 0.25)]
        return probs[0][0], probs[0][1], probs


async def test_short_low_confidence_match_is_skipped_unless_text_can_decide(whisper_cls):
    # Below the bar: no decode, no second encoder pass (would be dropped anyway).
    stt = _stt(whisper_cls, language=None, model=_UnsureHindiModel())
    result = await stt.finalize_stream(
        "s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"),
        require_language=("hi", 0.8),
    )
    assert result.text == "" and stt._model.calls == []
    # A bar of 0 (Hindi: Devanagari decides) decodes any matching blip.
    stt = _stt(whisper_cls, language=None, model=_UnsureHindiModel())
    result = await stt.finalize_stream(
        "s1", b"\x00\x01" * 8000, 16000, language=None, languages=("en", "hi"),
        require_language=("hi", 0.0),
    )
    assert result.text == "haan ji" and stt._model.calls[0]["language"] == "hi"
    assert result.language_confidence == pytest.approx(0.30 / 0.55)


def _stub_download(stt, monkeypatch, code=0):
    """Replace the download subprocess; returns the list of attempts."""
    attempts: list[int] = []

    async def download():
        attempts.append(1)
        if code:
            raise RuntimeError(f"model download exited {code}")

    monkeypatch.setattr(stt, "_download", download)
    return attempts


async def test_a_not_ready_instance_downloads_in_the_background_and_transcribes_once_loaded(whisper_cls, monkeypatch):
    stt = whisper_cls(model_size="small", language="en")
    loads: list[bool] = []

    def build(size, **kwargs):
        loads.append(kwargs["local_files_only"])
        return _StubModel()

    stt._WhisperModel = build
    _stub_download(stt, monkeypatch)
    stt.load_when_needed()  # what the provider factory does for a model that isn't on disk

    # Not ready yet: no model, so the call gets empty text rather than waiting.
    assert (await stt.transcribe(b"\x00\x01" * 8000, 16000)).text == ""

    await stt._load_task
    assert loads == [True]  # the subprocess downloaded; the load itself is local
    assert stt.loaded
    assert (await stt.transcribe(b"\x00\x01" * 8000, 16000)).text == "haan ji"


async def test_concurrent_transcribes_start_one_download(whisper_cls, monkeypatch):
    stt = whisper_cls(model_size="small", language="en")
    loads: list[bool] = []
    stt._WhisperModel = lambda size, **kw: loads.append(kw["local_files_only"]) or _StubModel()
    attempts = _stub_download(stt, monkeypatch)
    stt._download_on_demand = True

    await asyncio.gather(*(stt.transcribe(b"\x00\x01" * 8000, 16000) for _ in range(3)))
    await stt._load_task

    assert attempts == [1] and loads == [True]


async def test_a_failed_download_is_not_retried_until_the_backoff_passes(whisper_cls, monkeypatch):
    stt = whisper_cls(model_size="small", language="en")
    attempts = _stub_download(stt, monkeypatch, code=1)
    stt._download_on_demand = True
    now = [1000.0]
    from ..providers.stt import faster_whisper as fw
    monkeypatch.setattr(fw, "time", types.SimpleNamespace(monotonic=lambda: now[0]))

    await stt.transcribe(b"\x00\x01" * 8000, 16000)
    await stt._load_task
    for _ in range(3):  # more utterances during the backoff
        now[0] += 10
        await stt.transcribe(b"\x00\x01" * 8000, 16000)
    assert attempts == [1]

    now[0] += 60
    await stt.transcribe(b"\x00\x01" * 8000, 16000)
    await stt._load_task
    assert attempts == [1, 1]


async def test_stop_download_kills_an_in_flight_download_without_waiting_for_it(whisper_cls, monkeypatch):
    stt = whisper_cls(model_size="small", language="en")
    started = asyncio.Event()
    procs = []

    class _Proc:
        returncode = None

        async def wait(self):
            started.set()
            if self.returncode is None:
                await asyncio.Event().wait()  # a download that never finishes
            return self.returncode

        def kill(self):
            self.returncode = -9

    async def spawn(*argv):
        procs.append(_Proc())
        return procs[0]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    stt.load_when_needed()
    await started.wait()

    await asyncio.wait_for(stt.stop_download(), timeout=2)

    assert procs[0].returncode == -9


def test_whisper_has_no_aclose_for_the_bundle_to_call():
    # ProviderRegistry closes any not-ready provider exposing aclose() when a default stands in
    # (meant for unused Ollama clients); on Whisper that would cancel the shared download.
    from services.conversation.providers.stt.faster_whisper import FasterWhisperSTT

    assert not hasattr(FasterWhisperSTT, "aclose") and hasattr(FasterWhisperSTT, "stop_download")


async def test_whisper_aliases_share_one_download_key(whisper_cls, monkeypatch):
    from services.conversation.providers.stt import faster_whisper as fw

    keys = []

    async def fetch(key, *args):
        keys.append(key)

    monkeypatch.setattr(fw.model_downloads, "fetch", fetch)
    utils = type(sys)("faster_whisper.utils")
    utils._MODELS = {"large": "Systran/faster-whisper-large-v3", "large-v3": "Systran/faster-whisper-large-v3"}
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    for name in ("large", "large-v3"):
        await _stt(whisper_cls, None, model_size=name)._download()

    assert keys[0] == keys[1] == ("faster_whisper", "Systran/faster-whisper-large-v3")
