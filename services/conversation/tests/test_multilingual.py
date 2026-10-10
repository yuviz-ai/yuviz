"""Multilingual agents: LanguageTracker hysteresis + Hinglish rule, sentence splitting,
and end-to-end from Config SDK resolution to the language the providers receive."""

from __future__ import annotations

from typing import Any

import pytest

from libs.config_sdk import MockConfigProvider
from libs.config_sdk.workflow import starter_graph

from .. import language as lang_state
from ..language import LanguageTracker, UtteranceLanguage, reply_language_instruction, utterance_language
from ..pipeline import PipelineConversationHandler, _SENTENCE_RE
from ..provider_bundle import ProviderBundle
from ..providers.interfaces import INSTANCE_LANGUAGE, SttResult

U = UtteranceLanguage


# ── LanguageTracker ──────────────────────────────────────────────────────────

def test_first_confident_utterance_switches_immediately():
    t = LanguageTracker("en", ("en", "hi"))
    assert t.observe(U("hi", 0.9)) is True
    assert t.current == "hi"


def test_default_switches_on_one_confident_utterance():
    t = LanguageTracker("en", ("en", "hi"))
    t.observe(U("en", 0.9))
    assert t.observe(U("hi", 0.9)) is True
    assert t.observe(U("en", 0.9)) is True
    assert t.observe(U("hi", 0.5)) is False  # still needs confidence
    assert t.current == "en"


def test_first_utterance_in_default_language_establishes_it(monkeypatch):
    monkeypatch.setattr(lang_state, "SWITCH_STREAK", 2)
    t = LanguageTracker("en", ("en", "hi"))
    assert t.observe(U("en", 0.9)) is False
    # Now a single Hindi utterance must not flip it…
    assert t.observe(U("hi", 0.95)) is False
    assert t.current == "en"
    # …but a sustained switch does.
    assert t.observe(U("hi", 0.95)) is True
    assert t.current == "hi"


def test_noisy_or_unsupported_utterance_breaks_the_streak(monkeypatch):
    monkeypatch.setattr(lang_state, "SWITCH_STREAK", 2)
    t = LanguageTracker("en", ("en", "hi"))
    t.observe(U("en", 0.9))
    t.observe(U("hi", 0.9))
    t.observe(U("hi", 0.4))   # low confidence: ignored, streak reset
    t.observe(U("hi", 0.9))
    assert t.current == "en"
    t.observe(U("fr", 0.99))  # unsupported: ignored
    assert t.current == "en"
    assert t.detected == ["en", "hi"]


def test_unsupported_language_never_becomes_session_language():
    t = LanguageTracker("en", ("en", "hi"))
    for _ in range(5):
        assert t.observe(U("es", 0.99)) is False
    assert t.current == "en"
    assert t.detected == []


def test_streak_length_is_tunable(monkeypatch):
    monkeypatch.setattr(lang_state, "SWITCH_STREAK", 3)
    t = LanguageTracker("en", ("en", "hi"))
    t.observe(U("en", 0.9))
    assert not t.observe(U("hi", 0.9))
    assert not t.observe(U("hi", 0.9))
    assert t.observe(U("hi", 0.9))


def test_short_utterance_only_in_session_language():
    t = LanguageTracker("hi", ("en", "hi"))
    assert t.accept_short(U("hi", 0.9))
    assert not t.accept_short(U("hi", 0.7))   # below the short-utterance confidence
    assert not t.accept_short(U("en", 0.99))  # other language: dropped, never switches
    assert t.current == "hi"


# ── Hinglish rule ────────────────────────────────────────────────────────────

SUPPORTED = ("en", "hi")


def test_any_devanagari_counts_as_hindi():
    r = SttResult(text="मुझे appointment chahiye", language="en", language_confidence=0.6)
    assert utterance_language(r, SUPPORTED) == U("hi", 1.0)


def test_hi_word_share_at_threshold_counts_as_hindi():
    r = SttResult(text="mujhe appointment book karna", language="en", language_confidence=0.7,
                  language_shares={"en": 0.7, "hi": 0.3})
    assert utterance_language(r, SUPPORTED).language == "hi"


def test_hi_word_share_below_threshold_stays_english():
    r = SttResult(text="I want an appointment please ji", language="en", language_confidence=0.85,
                  language_shares={"en": 0.85, "hi": 0.15})
    assert utterance_language(r, SUPPORTED) == U("en", 0.85)


def test_hinglish_session_does_not_flip_flop(monkeypatch):
    monkeypatch.setattr(lang_state, "SWITCH_STREAK", 2)
    t = LanguageTracker("en", SUPPORTED)
    hinglish = SttResult(text="haan mujhe kal ka slot chahiye", language="en", language_confidence=0.6,
                         language_shares={"en": 0.4, "hi": 0.6})
    english = SttResult(text="tomorrow at five works", language="en", language_confidence=1.0,
                        language_shares={"en": 1.0})
    t.observe(utterance_language(hinglish, SUPPORTED))
    assert t.current == "hi"
    for r in (english, hinglish, english, hinglish):
        t.observe(utterance_language(r, SUPPORTED))
        assert t.current == "hi"
    # Two English-only utterances in a row is a real switch.
    t.observe(utterance_language(english, SUPPORTED))
    t.observe(utterance_language(english, SUPPORTED))
    assert t.current == "en"


def test_hinglish_rule_only_when_hindi_supported():
    r = SttResult(text="नमस्ते", language="hi", language_confidence=0.9)
    assert utterance_language(r, ("en", "es")) == U("hi", 0.9)


def test_reply_instruction_names_language_and_script():
    line = reply_language_instruction("hi")
    assert "Reply only in Hindi (हिन्दी)" in line and "Devanagari" in line
    assert "mix the same way" in line
    en = reply_language_instruction("en")
    assert "The caller is speaking English now. Reply only in English" in en
    # Anchored to the latest turn, so a mixed call can't pull the reply back to Hindi.
    assert "even if earlier turns of the call were in another language" in en
    assert "mix" not in en


# ── Sentence splitter ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, first, rest", [
    ("नमस्ते। आप कैसे हैं?", "नमस्ते।", "आप कैसे हैं?"),
    ("धन्यवाद॥ फिर मिलेंगे", "धन्यवाद॥", "फिर मिलेंगे"),
    ("こんにちは。元気ですか", "こんにちは。", "元気ですか"),
    ("好的！我们开始吧", "好的！", "我们开始吧"),
    ("真的？是的", "真的？", "是的"),
    ("Dr. Smith will see you. Okay", "Dr. Smith will see you.", "Okay"),
])
def test_sentence_splitter(text, first, rest):
    assert _SENTENCE_RE.split(text, maxsplit=1) == [first, rest]


def test_splitter_keeps_english_abbreviation_guards():
    assert len(_SENTENCE_RE.split("Talk to Mr. Rao now", maxsplit=1)) == 1


# ── End to end: Config SDK -> handler -> providers ───────────────────────────

class _RecordingSTT:
    accepts_language = True

    def __init__(self, results: list[SttResult]) -> None:
        self._results = list(results)
        self.languages: list[Any] = []

    async def feed_stream(self, session_id, chunk, sample_rate, *, language=INSTANCE_LANGUAGE):
        self.languages.append(language)

    async def finalize_stream(self, session_id, audio, sample_rate, *, language=INSTANCE_LANGUAGE):
        self.languages.append(language)
        return self._results.pop(0)

    async def cancel_stream(self, session_id):
        return None


class _RecordingTTS:
    accepts_language = True

    def __init__(self, name: str = "base") -> None:
        self.name = name
        self.calls: list[tuple[str, Any]] = []

    async def synthesize_stream(self, text, sample_rate, *, language=INSTANCE_LANGUAGE):
        self.calls.append((text, language))
        yield b"\x00\x00"


class _LegacyTTS:
    """Predates the language keyword entirely."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize_stream(self, text, sample_rate):
        self.texts.append(text)
        yield b"\x00\x00"


class _LLM:
    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    async def generate(self, messages):
        self.system_prompts.append(messages[0].content)
        yield "Okay."


async def _runtime(agent_kwargs: dict, stt_language=None, tts_language=None, tts_engine="cartesia"):
    provider = MockConfigProvider()
    provider.add_tenant(slug="acme", name="Acme")
    provider.add_provider_config(id="stt1", role="stt", engine="deepgram", model="nova-3", language=stt_language)
    provider.add_provider_config(id="llm1", role="llm", engine="openai")
    provider.add_provider_config(id="tts1", role="tts", engine=tts_engine, voice="v", language=tts_language)
    provider.add_provider_config(id="tts-hi", role="tts", engine="cartesia", voice="v-hi", tenant_id="tenant-id")
    provider.add_agent(
        "acme", slug="bot", name="Bot", stt_config_id="stt1", llm_config_id="llm1", tts_config_id="tts1",
        workflow=starter_graph("Hello!", "Be brief."), **agent_kwargs,
    )
    return await provider.get_runtime_config("acme", "bot")


def _handler(rc, stt, llm, tts, tts_by_language=None):
    bundle = ProviderBundle(stt=stt, llm=llm, tts=tts, tts_by_language=tts_by_language or {})
    return PipelineConversationHandler(rc, bundle)


_SPEECH = b"\x00" * 40_000   # 1.25 s at 16 kHz: clears every gate
_SHORT = b"\x00" * 20_000    # 0.625 s: multilingual short-utterance band
_BLIP = b"\x00" * 12_000     # 0.375 s: under the 0.45 s floor


async def _turn(handler, session_id, audio=_SPEECH):
    await handler.on_audio(session_id, b"\x00\x00")
    return [r async for r in handler.on_speech_ended(session_id, audio, 1000, -20.0)]


async def test_agent_language_reaches_stt_and_tts():
    rc = await _runtime({"language": "hi"}, stt_language="en", tts_language="en")
    stt = _RecordingSTT([SttResult(text="नमस्ते")])
    tts = _RecordingTTS()
    h = _handler(rc, stt, _LLM(), tts)

    await _turn(h, "s1")

    assert set(stt.languages) == {"hi"}
    assert tts.calls and all(lang == "hi" for _, lang in tts.calls)


async def test_single_language_agent_without_language_calls_providers_as_before():
    rc = await _runtime({}, stt_language="en", tts_language=None)
    stt = _RecordingSTT([SttResult(text="hello")])
    tts = _RecordingTTS()
    llm = _LLM()
    h = _handler(rc, stt, llm, tts)

    await _turn(h, "s1")

    assert set(stt.languages) == {INSTANCE_LANGUAGE}
    assert all(lang is INSTANCE_LANGUAGE for _, lang in tts.calls)
    assert "The caller is speaking" not in llm.system_prompts[0]


async def test_single_language_non_english_agent_gets_no_prompt_injection():
    rc = await _runtime({"language": "hi"})
    llm = _LLM()
    h = _handler(rc, _RecordingSTT([SttResult(text="नमस्ते")]), llm, _RecordingTTS())
    await _turn(h, "s1")
    assert "The caller is speaking" not in llm.system_prompts[0]


async def test_multilingual_switches_voice_prompt_and_stt_mode(monkeypatch):
    monkeypatch.setattr(lang_state, "SWITCH_STREAK", 2)
    rc = await _runtime({"language": "en", "supported_languages": ("en", "hi"),
                         "tts_config_by_language": {"hi": "tts-hi"}})
    assert rc.media.stt_language == "multi"
    stt = _RecordingSTT([
        SttResult(text="hello there", language="en", language_confidence=1.0, language_shares={"en": 1.0}),
        SttResult(text="मुझे अपॉइंटमेंट चाहिए", language="hi", language_confidence=1.0),
        SttResult(text="कल पाँच बजे", language="hi", language_confidence=1.0),
    ])
    base, hindi = _RecordingTTS("base"), _RecordingTTS("hi")
    llm = _LLM()
    h = _handler(rc, stt, llm, base, {"hi": hindi})

    await _turn(h, "s1")
    assert "Reply only in English" in llm.system_prompts[-1]
    assert base.calls and not hindi.calls

    await _turn(h, "s1")  # one Hindi utterance after English was established: no switch yet
    assert "Reply only in English" in llm.system_prompts[-1]

    await _turn(h, "s1")  # sustained: switch
    assert "Reply only in Hindi" in llm.system_prompts[-1]
    assert hindi.calls and all(lang == "hi" for _, lang in hindi.calls)
    assert set(stt.languages) == {"multi"}


async def test_multilingual_short_utterance_gate():
    rc = await _runtime({"language": "hi", "supported_languages": ("hi", "en")})
    stt = _RecordingSTT([
        SttResult(text="haan", language="hi", language_confidence=0.9),
        SttResult(text="Thank you.", language="en", language_confidence=0.99),
    ])
    llm = _LLM()
    h = _handler(rc, stt, llm, _RecordingTTS())

    assert await _turn(h, "s1", _BLIP) == []          # under the floor: STT never runs
    responses = await _turn(h, "s1", _SHORT)          # "haan" in session language: kept
    assert any(r.stt_text == "haan" for r in responses)
    responses = await _turn(h, "s1", _SHORT)          # confident English blip: dropped
    assert not any(r.stt_text for r in responses)
    assert h._session_language("s1") == "hi"


async def test_short_utterance_needs_stt_confidence_as_well_as_language():
    # Deepgram multi reports language_confidence 1.0 for any one-word transcript; a low
    # transcript confidence marks line noise that merely looks like the session language.
    rc = await _runtime({"language": "hi", "supported_languages": ("hi", "en")})
    stt = _RecordingSTT([
        SttResult(text="हाँ", confidence=0.3, language="hi", language_confidence=1.0),
        SttResult(text="हाँ", confidence=0.95, language="hi", language_confidence=1.0),
    ])
    h = _handler(rc, stt, _LLM(), _RecordingTTS())
    assert not any(r.stt_text for r in await _turn(h, "s1", _SHORT))
    assert any(r.stt_text == "हाँ" for r in await _turn(h, "s1", _SHORT))


async def test_short_speech_bar_has_its_own_setting(monkeypatch):
    monkeypatch.setattr(lang_state, "SHORT_MIN_SPEECH_CONFIDENCE", 0.2)
    monkeypatch.setattr(lang_state, "SHORT_MIN_CONFIDENCE", 0.99)  # the language bar must not apply to it
    rc = await _runtime({"language": "hi", "supported_languages": ("hi", "en")})
    stt = _RecordingSTT([SttResult(text="हाँ", confidence=0.3, language="hi", language_confidence=1.0)])
    h = _handler(rc, stt, _LLM(), _RecordingTTS())
    assert any(r.stt_text == "हाँ" for r in await _turn(h, "s1", _SHORT))


def test_short_bar_defaults_without_env():
    import os, subprocess, sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICEAI_")}
    out = subprocess.run(
        [sys.executable, "-c",
         "from services.conversation import language as l;"
         "print(l.SHORT_MIN_CONFIDENCE_HI, l.SHORT_MIN_SPEECH_CONFIDENCE)"],
        env=env, capture_output=True, text=True, check=True, cwd=os.getcwd(),
    ).stdout.split()
    assert out == ["0.0", "0.8"]


async def test_single_language_keeps_one_second_gate():
    rc = await _runtime({})
    stt = _RecordingSTT([SttResult(text="haan", language="hi", language_confidence=0.99)])
    h = _handler(rc, stt, _LLM(), _RecordingTTS())
    assert await _turn(h, "s1", _SHORT) == []


async def test_legacy_tts_without_language_keyword_still_works():
    rc = await _runtime({"language": "en", "supported_languages": ("en", "hi")})
    tts = _LegacyTTS()
    h = _handler(rc, _RecordingSTT([SttResult(text="hello", language="en", language_confidence=1.0)]), _LLM(), tts)
    await _turn(h, "s1")
    assert tts.texts


# ── Localised greeting and system strings ────────────────────────────────────

async def test_greeting_uses_default_language_greeting_map():
    rc = await _runtime({"language": "hi", "supported_languages": ("hi", "en"),
                         "greeting_by_language": {"hi": "नमस्ते, मैं {{agent_name}} हूँ।", "en": "Hi!"}})
    tts = _RecordingTTS()
    h = _handler(rc, _RecordingSTT([]), _LLM(), tts)
    await h.greeting("s1")
    assert tts.calls == [("नमस्ते, मैं Bot हूँ।", "hi")]


async def test_greeting_without_map_uses_workflow_greeting():
    rc = await _runtime({"language": "hi", "supported_languages": ("hi", "en")})
    tts = _RecordingTTS()
    h = _handler(rc, _RecordingSTT([]), _LLM(), tts)
    await h.greeting("s1")
    assert tts.calls == [("Hello!", "hi")]


async def test_first_turn_filler_follows_session_language():
    rc = await _runtime({"language": "en", "supported_languages": ("en", "hi")})
    tts = _RecordingTTS()
    stt = _RecordingSTT([SttResult(text="नमस्ते जी", language="hi", language_confidence=1.0)])
    h = _handler(rc, stt, _LLM(), tts)
    await _turn(h, "s1")
    assert tts.calls[0] == ("जी, एक पल।", "hi")


async def test_farewell_message_override_still_wins():
    rc = await _runtime({"language": "hi", "supported_languages": ("hi", "en"), "farewell_message": "Bye from Acme."})
    assert rc.conversation.farewell_message == "Bye from Acme."
    h = _handler(rc, _RecordingSTT([]), _LLM(), _RecordingTTS())
    assert h._farewell_message == "Bye from Acme."


# ── Booking-claim check per language ─────────────────────────────────────────

from ..pipeline import _claims_booking_without_tool_call  # noqa: E402


@pytest.mark.parametrize("text", [
    "आपका अपॉइंटमेंट बुक हो गया है।",
    "Aapka appointment book ho gaya hai.",
    "आपका appointment confirm कर दिया है।",
    "Your appointment is confirmed.",
])
def test_booking_claim_detected_in_hindi_session(text):
    assert _claims_booking_without_tool_call(text, "hi")


def test_booking_claim_hindi_precision():
    assert not _claims_booking_without_tool_call("क्या मैं आपका अपॉइंटमेंट बुक कर दूँ?", "hi")


def test_booking_claim_skipped_for_unknown_language():
    assert not _claims_booking_without_tool_call("Your appointment is confirmed.", "fr")
    assert _claims_booking_without_tool_call("Your appointment is confirmed.")


async def test_guardrail_skip_is_logged_once_per_session(caplog):
    caplog.set_level("INFO")
    rc = await _runtime({"language": "en", "supported_languages": ("en", "es")})
    stt = _RecordingSTT([
        SttResult(text="hola buenos días", language="es", language_confidence=1.0),
        SttResult(text="esto es inútil", language="es", language_confidence=1.0),
    ])
    h = _handler(rc, stt, _LLM(), _RecordingTTS())
    await _turn(h, "s1")
    await _turn(h, "s1")
    skipped = [r for r in caplog.records if "guardrail and booking-claim checks skipped" in r.message]
    assert len(skipped) == 1


# ── Post-call ────────────────────────────────────────────────────────────────

def test_post_call_prompts_require_english():
    from ..sentiment import _SYSTEM_PROMPT
    from ..session_finalizer import _SUMMARY_PROMPT
    for prompt in (_SYSTEM_PROMPT, _SUMMARY_PROMPT):
        assert "English, whatever language the call was in" in prompt.replace("\n", " ")


async def test_detected_languages_recorded_at_session_end():
    from unittest.mock import MagicMock

    rc = await _runtime({"language": "en", "supported_languages": ("en", "hi")})
    stt = _RecordingSTT([SttResult(text="नमस्ते", language="hi", language_confidence=1.0)])
    transcripts = MagicMock()
    h = PipelineConversationHandler(rc, ProviderBundle(stt=stt, llm=_LLM(), tts=_RecordingTTS()),
                                    transcripts=transcripts)
    await _turn(h, "s1")
    await h.on_session_end("s1", "hangup")
    transcripts.record_detected_languages.assert_called_once_with("s1", ["hi"])


async def test_single_language_agent_never_records_languages():
    from unittest.mock import MagicMock

    rc = await _runtime({})
    transcripts = MagicMock()
    h = PipelineConversationHandler(
        rc, ProviderBundle(stt=_RecordingSTT([SttResult(text="hi")]), llm=_LLM(), tts=_RecordingTTS()),
        transcripts=transcripts,
    )
    await _turn(h, "s1")
    await h.on_session_end("s1", "hangup")
    transcripts.record_detected_languages.assert_not_called()


# ── Fixes from local testing: Urdu-labelled Hindi, script-mismatched replies ──

from ..language import script_language  # noqa: E402


def test_urdu_detection_counts_as_hindi():
    r = SttResult(text="mujhe madad chahiye", language="ur", language_confidence=0.9)
    assert utterance_language(r, ("en", "hi")) == U("hi", 0.9)
    assert utterance_language(r, ("en", "es")).language == "ur"


def test_script_language():
    assert script_language("मुझे खेद है, लेकिन मैं मदद करूँगी।", ("en", "hi")) == "hi"
    assert script_language("Your appointment is at 5.", ("en", "hi")) is None
    assert script_language("Okay, नमस्ते and welcome to our clinic today", ("en", "hi")) is None
    assert script_language("मुझे खेद है", ("en", "es")) is None
    assert script_language("こんにちは、元気ですか", ("en", "ja", "zh")) == "ja"
    assert script_language("你好，欢迎", ("en", "zh")) == "zh"


async def test_devanagari_reply_in_english_session_uses_hindi_voice():
    rc = await _runtime({"language": "en", "supported_languages": ("en", "hi")})
    stt = _RecordingSTT([SttResult(text="I want to talk in Hindi.", language="en", language_confidence=1.0)])
    base, hindi = _RecordingTTS("base"), _RecordingTTS("hi")

    class _HindiLLM(_LLM):
        async def generate(self, messages):
            yield "ज़रूर, हम हिंदी में बात कर सकते हैं।"

    h = _handler(rc, stt, _HindiLLM(), base, {"hi": hindi})
    await _turn(h, "s1")
    assert h._session_language("s1") == "en"
    assert hindi.calls == [("ज़रूर, हम हिंदी में बात कर सकते हैं।", "hi")]


async def test_whisper_style_stt_gets_candidate_languages():
    class _CandidateSTT(_RecordingSTT):
        accepts_language_candidates = True

        async def finalize_stream(self, session_id, audio, sample_rate, *, language=INSTANCE_LANGUAGE, languages=None):
            self.languages.append((language, languages))
            return self._results.pop(0)

        async def feed_stream(self, session_id, chunk, sample_rate, *, language=INSTANCE_LANGUAGE, languages=None):
            return None

    provider = MockConfigProvider()
    provider.add_tenant(slug="acme", name="Acme")
    provider.add_provider_config(id="stt1", role="stt", engine="faster_whisper", model="small")
    provider.add_provider_config(id="llm1", role="llm", engine="openai")
    provider.add_provider_config(id="tts1", role="tts", engine="kokoro", voice="af_sarah")
    provider.add_agent("acme", slug="bot", name="Bot", stt_config_id="stt1", llm_config_id="llm1",
                       tts_config_id="tts1", workflow=starter_graph("Hi", "Be brief."),
                       language="en", supported_languages=("en", "hi"))
    rc = await provider.get_runtime_config("acme", "bot")
    stt = _CandidateSTT([SttResult(text="नमस्ते", language="hi", language_confidence=0.9)])
    await _turn(_handler(rc, stt, _LLM(), _RecordingTTS()), "s1")
    assert stt.languages == [(None, ("en", "hi"))]


# ── Review fixes: short-blip early reject, single override rule ──────────────

async def test_short_audio_asks_whisper_to_reject_before_decoding():
    class _CandidateSTT(_RecordingSTT):
        accepts_language_candidates = True

        async def finalize_stream(self, session_id, audio, sample_rate, **kwargs):
            self.languages.append(kwargs)
            return self._results.pop(0)

        async def feed_stream(self, session_id, chunk, sample_rate, **kwargs):
            return None

    provider = MockConfigProvider()
    provider.add_tenant(slug="acme", name="Acme")
    provider.add_provider_config(id="stt1", role="stt", engine="faster_whisper", model="small")
    provider.add_provider_config(id="llm1", role="llm", engine="openai")
    provider.add_provider_config(id="tts1", role="tts", engine="kokoro", voice="hf_alpha")
    provider.add_agent("acme", slug="bot", name="Bot", stt_config_id="stt1", llm_config_id="llm1",
                       tts_config_id="tts1", workflow=starter_graph("Hi", "Be brief."),
                       language="hi", supported_languages=("hi", "en"))
    rc = await provider.get_runtime_config("acme", "bot")
    stt = _CandidateSTT([SttResult(text="", language="en", language_confidence=0.99),
                         SttResult(text="haan", language="hi", language_confidence=0.9)])
    h = _handler(rc, stt, _LLM(), _RecordingTTS())
    assert await _turn(h, "s1", _SHORT) == []  # rejected inside STT: nothing heard
    await _turn(h, "s1", _SPEECH)
    # Hindi session: any matching blip is decoded (Devanagari text decides), so the bar is 0.
    assert stt.languages[0]["require_language"] == ("hi", 0.0)
    assert "require_language" not in stt.languages[1]


async def test_hindi_short_audio_uses_the_hindi_bar_setting(monkeypatch):
    class _CandidateSTT(_RecordingSTT):
        accepts_language_candidates = True

        async def finalize_stream(self, session_id, audio, sample_rate, **kwargs):
            self.languages.append(kwargs)
            return self._results.pop(0)

        async def feed_stream(self, session_id, chunk, sample_rate, **kwargs):
            return None

    monkeypatch.setattr(lang_state, "SHORT_MIN_CONFIDENCE_HI", 0.9)
    provider = MockConfigProvider()
    provider.add_tenant(slug="acme", name="Acme")
    provider.add_provider_config(id="stt1", role="stt", engine="faster_whisper", model="small")
    provider.add_provider_config(id="llm1", role="llm", engine="openai")
    provider.add_provider_config(id="tts1", role="tts", engine="kokoro", voice="hf_alpha")
    provider.add_agent("acme", slug="bot", name="Bot", stt_config_id="stt1", llm_config_id="llm1",
                       tts_config_id="tts1", workflow=starter_graph("Hi", "Be brief."),
                       language="hi", supported_languages=("hi", "en"))
    rc = await provider.get_runtime_config("acme", "bot")
    stt = _CandidateSTT([SttResult(text="", language="hi", language_confidence=0.5)])
    await _turn(_handler(rc, stt, _LLM(), _RecordingTTS()), "s1", _SHORT)
    assert stt.languages[0]["require_language"] == ("hi", 0.9)


async def test_pipeline_uses_the_bundles_override_rule():
    rc = await _runtime({"language": "hi", "supported_languages": ("hi", "en")})
    seen: list = []

    class _Bundle(ProviderBundle):
        def tts_for(self, language):
            seen.append(language)
            return super().tts_for(language)

    tts = _RecordingTTS()
    h = PipelineConversationHandler(rc, _Bundle(stt=_RecordingSTT([]), llm=_LLM(), tts=tts))
    await h.greeting("s1")
    assert seen == ["hi"] and tts.calls



async def test_short_audio_in_english_session_asks_for_the_confidence_bar():
    class _CandidateSTT(_RecordingSTT):
        accepts_language_candidates = True

        async def finalize_stream(self, session_id, audio, sample_rate, **kwargs):
            self.languages.append(kwargs)
            return self._results.pop(0)

        async def feed_stream(self, session_id, chunk, sample_rate, **kwargs):
            return None

    provider = MockConfigProvider()
    provider.add_tenant(slug="acme", name="Acme")
    provider.add_provider_config(id="stt1", role="stt", engine="faster_whisper", model="small")
    provider.add_provider_config(id="llm1", role="llm", engine="openai")
    provider.add_provider_config(id="tts1", role="tts", engine="kokoro", voice="af_heart")
    provider.add_agent("acme", slug="bot", name="Bot", stt_config_id="stt1", llm_config_id="llm1",
                       tts_config_id="tts1", workflow=starter_graph("Hi", "Be brief."),
                       language="en", supported_languages=("en", "hi"))
    rc = await provider.get_runtime_config("acme", "bot")
    stt = _CandidateSTT([SttResult(text="", language="en", language_confidence=0.5)])
    assert await _turn(_handler(rc, stt, _LLM(), _RecordingTTS()), "s1", _SHORT) == []
    assert stt.languages[0]["require_language"] == ("en", lang_state.SHORT_MIN_CONFIDENCE)
