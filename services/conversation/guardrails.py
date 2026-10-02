"""GuardrailDetector: regex-only caller frustration/abuse detection (no I/O, hot-path
safe). Curated English lexicon tuned for precision over recall."""

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

_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("frustration", re.compile(r"\b(?:" + "|".join(_FRUSTRATION_PHRASES) + r")\b", re.IGNORECASE)),
    ("abuse",       re.compile(r"\b(?:" + "|".join(_ABUSE_PHRASES) + r")\b", re.IGNORECASE)),
]


@dataclass(frozen=True)
class GuardrailViolation:
    category: str  # "frustration" | "abuse"
    matched:  str  # the exact text span that fired — for logs, never spoken


class GuardrailDetector:
    """check() returns the first violation in an utterance, or None (at most one per utterance)."""

    @staticmethod
    def check(text: str) -> GuardrailViolation | None:
        if not text:
            return None
        for category, pattern in _CATEGORIES:
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
