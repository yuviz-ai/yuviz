"""GuardrailDetector: deterministic inline frustration/abuse phrase matching."""

from __future__ import annotations

import pytest

from ..guardrails import GuardrailDetector


def test_no_violation_on_ordinary_speech():
    assert GuardrailDetector.check("Can you help me check my order status?") is None


def test_empty_text_no_violation():
    assert GuardrailDetector.check("") is None


def test_frustration_phrase_detected():
    v = GuardrailDetector.check("This is useless, you're not helping at all.")
    assert v is not None
    assert v.category == "frustration"


def test_frustration_repetition_complaint_detected():
    v = GuardrailDetector.check("I already told you my account number twice.")
    assert v is not None
    assert v.category == "frustration"


def test_abuse_phrase_detected():
    v = GuardrailDetector.check("This is fucking ridiculous.")
    assert v is not None
    # Both categories match; only the first (list order) is returned, one violation per utterance.
    assert v.category in ("abuse", "frustration")


def test_case_insensitive_matching():
    v = GuardrailDetector.check("SHUT UP and just transfer me.")
    assert v is not None
    assert v.category == "abuse"


def test_word_boundary_avoids_false_positive_substrings():
    # "classic" contains no guardrail phrase; "assholeworthy" is not a real
    # word but exercises that matching requires a clean word boundary.
    assert GuardrailDetector.check("That's a classic move.") is None


def test_matched_span_is_the_triggering_text():
    v = GuardrailDetector.check("Stop repeating yourself, this is pointless.")
    assert v is not None
    assert v.matched.lower() in ("stop repeating", "this is pointless")


def test_one_violation_per_utterance_even_with_multiple_hits():
    v = GuardrailDetector.check("This is useless. This is ridiculous. I give up.")
    assert v is not None  # exactly one GuardrailViolation object, not a list


# ---------------------------------------------------------------------------
# GuardrailCounter: per-session consecutive count, separate from the engine
# ---------------------------------------------------------------------------

from ..guardrails import GuardrailCounter


def test_counter_increments_per_session():
    counter = GuardrailCounter()
    assert counter.increment("s1") == 1
    assert counter.increment("s1") == 2
    assert counter.increment("s1") == 3


def test_counter_is_independent_per_session():
    counter = GuardrailCounter()
    counter.increment("s1")
    counter.increment("s1")
    assert counter.increment("s2") == 1
    assert counter.current("s1") == 2


def test_counter_reset_zeroes_current_but_next_increment_starts_at_one():
    counter = GuardrailCounter()
    counter.increment("s1")
    counter.increment("s1")
    counter.reset("s1")
    assert counter.current("s1") == 0
    assert counter.increment("s1") == 1


def test_counter_current_on_unseen_session_is_zero():
    counter = GuardrailCounter()
    assert counter.current("never-seen") == 0


def test_counter_forget_clears_like_reset():
    counter = GuardrailCounter()
    counter.increment("s1")
    counter.forget("s1")
    assert counter.current("s1") == 0


def test_expanded_lexicon_from_live_call_misses():
    # Real phrases from live calls that v1 missed.
    assert GuardrailDetector.check("because I am not satisfied with this idea.") is not None
    assert GuardrailDetector.check("This just isn't working for me.") is not None
    assert GuardrailDetector.check("That doesn't help at all.") is not None
    assert GuardrailDetector.check("Your answer is not helpful.") is not None
    # Still precise: ordinary sentences with nearby words don't fire.
    assert GuardrailDetector.check("I'm working on my new business plan.") is None
    assert GuardrailDetector.check("Can you help me with shipping rates?") is None


def test_intensified_ridiculous_useless_variants_match():
    assert GuardrailDetector.check("your service is completely ridiculous") is not None
    assert GuardrailDetector.check("that was absolutely useless") is not None
    assert GuardrailDetector.check("The ridiculous thing about pricing is complexity.") is None


# ── Per-language lexicons ────────────────────────────────────────────────────

def test_language_none_is_english_as_before():
    assert GuardrailDetector.check("this is useless", None) is not None
    assert GuardrailDetector.check("this is useless", "en") is not None


@pytest.mark.parametrize("text, category", [
    ("यह बेकार है", "frustration"),
    ("आप समझ नहीं रहे", "frustration"),
    ("मैंने पहले ही बताया था", "frustration"),
    ("bhai ye bekar hai", "frustration"),
    ("aap samajh nahi rahe", "frustration"),
    ("main tang aa gaya hoon", "frustration"),
    ("चुप रहो", "abuse"),
    ("kya bakwas hai", "abuse"),
    ("this is useless yaar", "frustration"),  # English lexicon still applies to Hinglish
])
def test_hindi_session_matches_hindi_and_english(text, category):
    v = GuardrailDetector.check(text, "hi")
    assert v is not None and v.category == category


@pytest.mark.parametrize("text", [
    "मुझे कल का अपॉइंटमेंट चाहिए",
    "haan ji, paanch baje theek hai",
    "kaam ho gaya, dhanyavaad",
    "bekarar hoon milne ko",  # bekar- inside a longer word must not fire
])
def test_hindi_lexicon_precision(text):
    assert GuardrailDetector.check(text, "hi") is None


def test_hindi_lexicon_not_used_for_english_sessions():
    assert GuardrailDetector.check("यह बेकार है", "en") is None


@pytest.mark.parametrize("language", ["fr", "ja", "xx"])
def test_unknown_language_fails_safe(language):
    assert not GuardrailDetector.supports(language)
    assert GuardrailDetector.check("this is fucking useless", language) is None


def test_romanised_hindi_abuse_does_not_flag_english_sale():
    assert GuardrailDetector.check("kya sale abhi chal rahi hai", "hi") is None
    assert GuardrailDetector.check("saale", "hi") is not None
