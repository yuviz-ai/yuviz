"""GuardrailDetector: regex-only caller frustration/abuse detection (no I/O, hot-path
safe). Curated lexicons tuned for precision over recall, selected by the session
language: English, and for Hindi English + Devanagari + romanised Hindi (callers
code-switch). Any other language has no lexicon and is never checked, so it can't
cause a false escalation."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Explicit frustration with the AI/conversation — the caller is telling us
# the interaction is failing.
_FRUSTRATION_PHRASES = [
    r"this is useless",
    r"this is ridiculous",
    r"this is pointless",
    r"(?:you'?re|you are) not helping",
    r"not helping at all",
    r"(?:you'?re|you are) not listening",
    r"you don'?t understand",
    r"i already told you",
    r"you keep saying the same",
    r"i give up",
    r"waste of (?:my )?time",
    r"(?:i'?m|i am) (?:getting )?frustrated",
    r"this is frustrating",
    r"sick of this",
    r"fed up",
    r"stop repeating",
    r"not satisfied",
    r"not helpful",
    r"(?:isn'?t|is not|not) working",
    r"doesn'?t work",
    r"doesn'?t help",
    # e.g. "your service is completely ridiculous"
    r"(?:is|was) (?:completely|absolutely|totally|just) (?:ridiculous|useless)",
]

# Abuse / profanity directed at the agent — a strong signal the caller is
# past the point where the AI should keep trying alone.
_ABUSE_PHRASES = [
    r"fuck(?:ing|ed)?",
    r"shit",
    r"bullshit",
    r"bastard",
    r"asshole",
    r"idiot",
    r"stupid (?:bot|machine|robot|thing|ai)",
    r"shut up",
    r"piece of (?:crap|junk|garbage)",
]

# Hindi, in Devanagari and romanised. \b is unreliable around Devanagari vowel signs,
# so these use explicit letter boundaries.
_HI_FRUSTRATION_PHRASES = [
    r"बेकार है", r"कोई (?:फ़ायदा|फायदा) नहीं", r"(?:आप|तुम) समझ नहीं (?:रहे|रही)",
    r"(?:मैंने|मै ने) पहले (?:ही )?(?:बताया|बोला)", r"बार[- ]बार (?:बोल|बता|पूछ)",
    r"परेशान (?:हो )?(?:गया|गई|गयी)", r"तंग आ (?:गया|गई|गयी)", r"(?:समय|टाइम) (?:की )?बर्बादी",
    r"मदद नहीं कर (?:रहे|रही|रहा)", r"काम नहीं कर (?:रहा|रही)", r"सुन नहीं (?:रहे|रही|रहा)",
    r"beka+r hai", r"koi fa[iy]da nahi?", r"samajh nahi?n? (?:rahe|rahi|raha)",
    r"maine pehle (?:hi )?(?:bataya|bola)", r"baar baar (?:bol|bata|pooch|puch)",
    r"pareshan (?:ho )?(?:gaya|gayi)", r"tang aa (?:gaya|gayi)", r"time waste",
    r"madad nahi?n? kar (?:rahe|rahi|raha)", r"kaam nahi?n? kar (?:raha|rahi)",
    r"sun nahi?n? (?:rahe|rahi|raha)",
]
_HI_ABUSE_PHRASES = [
    r"बकवास", r"चुप (?:रहो|कर|करो)", r"बेवकू(?:फ़|फ)", r"साला", r"साले", r"कमीन(?:ा|े)", r"हरामी",
    r"भाड़ में जा(?:ओ)?",
    r"bakwa(?:a)?s", r"chup (?:raho|kar|karo)", r"be?w[ae]?koo?f", r"saale", r"saala",
    r"kamine?a?", r"harami", r"bhaa?d (?:me|mein) jao?",
]

_LETTER = r"[\w\u0900-\u097F]"


def _compile(phrases: list[str]) -> re.Pattern[str]:
    return re.compile(rf"(?<!{_LETTER})(?:" + "|".join(phrases) + rf")(?!{_LETTER})", re.IGNORECASE)


_EN_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("frustration", re.compile(r"\b(?:" + "|".join(_FRUSTRATION_PHRASES) + r")\b", re.IGNORECASE)),
    ("abuse",       re.compile(r"\b(?:" + "|".join(_ABUSE_PHRASES) + r")\b", re.IGNORECASE)),
]
_HI_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("frustration", _compile(_HI_FRUSTRATION_PHRASES)),
    ("abuse",       _compile(_HI_ABUSE_PHRASES)),
]

# Session language -> lexicons checked, in order.
_LEXICONS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "en": _EN_CATEGORIES,
    "hi": _EN_CATEGORIES + _HI_CATEGORIES,
}
# Kept for callers that predate language selection.
_CATEGORIES = _EN_CATEGORIES


@dataclass(frozen=True)
class GuardrailViolation:
    category: str  # "frustration" | "abuse"
    matched:  str  # the exact text span that fired — for logs, never spoken


class GuardrailDetector:
    """check() returns the first violation in an utterance, or None (at most one per utterance).
    language=None means English, as before languages existed."""

    @staticmethod
    def supports(language: str | None) -> bool:
        return (language or "en") in _LEXICONS

    @staticmethod
    def check(text: str, language: str | None = None) -> GuardrailViolation | None:
        if not text:
            return None
        # Unknown language: fail safe (no violation). The caller logs the skip.
        for category, pattern in _LEXICONS.get(language or "en", []):
            m = pattern.search(text)
            if m:
                return GuardrailViolation(category=category, matched=m.group(0))
        return None


class GuardrailCounter:
    """Per-session consecutive violation count; reset() on any clean turn.
    Thresholds/transfer decisions live in TransferDecisionEngine."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def increment(self, session_id: str) -> int:
        count = self._counts.get(session_id, 0) + 1
        self._counts[session_id] = count
        return count

    def reset(self, session_id: str) -> None:
        self._counts.pop(session_id, None)

    def current(self, session_id: str) -> int:
        return self._counts.get(session_id, 0)

    # Session-end alias for reset.
    forget = reset
