"""Session language for multilingual agents: which language the caller is speaking,
decided per utterance from the STT result we already have (no extra calls).

Pure and hot-path safe. Thresholds are module constants, each overridable by env
(VOICEAI_LANG_*) so they can be tuned on real calls without a code change; they are
read at call time, so tests can monkeypatch them.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from libs.config_sdk.languages import LANGUAGES, language_name, normalize_language, resolve_alias

from .providers.interfaces import SttResult

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        log.warning("%s=%r is not a number — using %s", name, os.environ.get(name), default)
        return default


# Below this the detected language is ignored (and breaks a switch streak).
MIN_CONFIDENCE = _env_float("VOICEAI_LANG_MIN_CONFIDENCE", 0.70)
# Consecutive confident utterances in a new language needed to switch to it. 1 = the reply
# follows the caller's latest language; 2+ = hysteresis against one misheard utterance.
SWITCH_STREAK = int(_env_float("VOICEAI_LANG_SWITCH_STREAK", 1))
# Hinglish: an utterance counts as Hindi at or above this share of hi-tagged words.
HI_WORD_SHARE = _env_float("VOICEAI_LANG_HI_WORD_SHARE", 0.30)
# Short utterances (multilingual agents): always dropped below SHORT_MIN_S; between it
# and the 1.0 s floor, kept only at SHORT_MIN_CONFIDENCE in the session's language.
SHORT_MIN_S = _env_float("VOICEAI_LANG_SHORT_MIN_S", 0.45)
SHORT_MIN_CONFIDENCE = _env_float("VOICEAI_LANG_SHORT_MIN_CONFIDENCE", 0.80)
# Short utterances also need the STT engine's own transcript confidence (Deepgram) at this
# level: the language checks measure language, not whether it was speech.
SHORT_MIN_SPEECH_CONFIDENCE = _env_float("VOICEAI_STT_SHORT_MIN_SPEECH_CONFIDENCE", 0.80)
# Language bar for the pre-decode check in a Hindi session: 0 lets Whisper decode so Devanagari
# text can decide. Only the language bar; the speech-signal checks still apply.
SHORT_MIN_CONFIDENCE_HI = _env_float("VOICEAI_LANG_SHORT_MIN_CONFIDENCE_HI", 0.0)

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Scripts that identify one registry language on their own (Latin is shared, so absent).
_SCRIPT_LANGUAGES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hi", _DEVANAGARI),
    ("ja", re.compile(r"[\u3040-\u30ff]")),           # kana: Japanese even when mixed with kanji
    ("zh", re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")),
)
# A sentence is voiced in its script's language when that script is this share of its letters.
_SCRIPT_SHARE = 0.5


@dataclass(frozen=True)
class UtteranceLanguage:
    language:   str | None
    confidence: float | None


def utterance_language(result: SttResult, supported: tuple[str, ...]) -> UtteranceLanguage:
    """The utterance's language after the Hinglish rule: when Hindi is supported, any
    Devanagari or >= HI_WORD_SHARE hi-tagged words makes it Hindi; otherwise the
    STT's own detection stands (so mixed hi/en below the share is English)."""
    detected = resolve_alias(normalize_language(result.language), supported)
    if "hi" in supported:
        if _DEVANAGARI.search(result.text or ""):
            return UtteranceLanguage("hi", 1.0)
        shares = result.language_shares
        if shares and shares.get("hi", 0.0) >= HI_WORD_SHARE:
            return UtteranceLanguage("hi", 1.0)
    return UtteranceLanguage(detected, result.language_confidence)


class LanguageTracker:
    """Per-session language state with hysteresis.

    - The first confident utterance of the call switches immediately (the default
      language is only a prior).
    - After that, a switch needs SWITCH_STREAK (default 1) consecutive confident utterances in the
      same new language, so one noisy utterance can't flip it.
    - Unsupported or low-confidence utterances change nothing and break a streak.
    """

    def __init__(self, default: str, supported: tuple[str, ...]) -> None:
        self.current = default
        self._supported = supported
        self._candidate: str | None = None
        self._streak = 0
        self._has_evidence = False
        # Confident supported languages heard, in order of first appearance.
        self.detected: list[str] = []

    def observe(self, utterance: UtteranceLanguage, session_id: str = "") -> bool:
        """Feed one (non-short) utterance; returns True when the session language switched."""
        lang, conf = utterance.language, utterance.confidence
        if lang not in self._supported or conf is None or conf < MIN_CONFIDENCE:
            if lang is not None and lang not in self._supported:
                log.debug("language %s not supported — staying in %s session=%s", lang, self.current, session_id)
            self._candidate, self._streak = None, 0
            return False
        if lang not in self.detected:
            self.detected.append(lang)

        if lang == self.current:
            self._has_evidence = True
            self._candidate, self._streak = None, 0
            return False
        if not self._has_evidence:
            self._has_evidence = True
            return self._switch(lang, "first utterance", session_id)
        if lang == self._candidate:
            self._streak += 1
        else:
            self._candidate, self._streak = lang, 1
        if self._streak >= SWITCH_STREAK:
            return self._switch(lang, f"{self._streak} consecutive utterances", session_id)
        return False

    def accept_short(self, utterance: UtteranceLanguage) -> bool:
        """A short utterance passes only in the session's own language at high confidence.
        It is never fed to observe(), so it can never switch the language."""
        return (
            utterance.language == self.current
            and utterance.confidence is not None
            and utterance.confidence >= SHORT_MIN_CONFIDENCE
        )

    def _switch(self, lang: str, why: str, session_id: str) -> bool:
        log.info("Session language %s -> %s (%s) session=%s", self.current, lang, why, session_id)
        self.current = lang
        self._candidate, self._streak = None, 0
        return True


def script_language(text: str, supported: tuple[str, ...]) -> str | None:
    """The supported language a sentence's script unambiguously belongs to, if any.
    Guards TTS when the LLM answers in another language than the session's: Devanagari
    read by an English voice is unintelligible."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    for lang, pattern in _SCRIPT_LANGUAGES:
        if lang in supported and sum(1 for c in letters if pattern.match(c)) / len(letters) >= _SCRIPT_SHARE:
            return lang
    return None


def reply_language_instruction(language: str) -> str:
    """Per-turn system-prompt line for multilingual agents (never single-language ones).

    Anchored to the caller's latest language: a generic "mirror the caller's mix" let small
    models (llama3.2) keep answering in Hindi after the caller switched back to English,
    because the call as a whole was mixed."""
    lang = LANGUAGES.get(language)
    name = f"{lang.name} ({lang.native_name})" if lang and lang.native_name != lang.name else language_name(language)
    plain = lang.name if lang else language
    mix = (
        f"; if the caller mixes {plain} and English in their sentences, you may mix the same way"
        if language != "en" else ""
    )
    hint = f" {lang.script_hint}" if lang and lang.script_hint else ""
    return (
        f"\n\nThe caller is speaking {plain} now. Reply only in {name}, even if earlier turns "
        f"of the call were in another language{mix}.{hint} "
        "Keep any [[...]] tokens, numbers and tool arguments exactly as specified."
    )
