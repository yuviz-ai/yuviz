"""Spoken system strings per language, falling back to English."""

from __future__ import annotations

import pytest

from ..fillers import FillerSelector, _TOOL_FILLERS
from ..i18n import en, hi, t, tool_fillers
from .. import pipeline


def test_every_hindi_key_exists_in_english():
    assert set(hi.STRINGS) <= set(en.STRINGS)


def test_hindi_strings_are_used_for_hindi():
    assert t("fallback_goodbye", "hi") == hi.STRINGS["fallback_goodbye"]
    assert t("first_turn_filler", "hi-IN") == hi.STRINGS["first_turn_filler"]


@pytest.mark.parametrize("language", ["fr", "xx", None, ""])
def test_unknown_language_falls_back_to_english(language):
    assert t("fallback_goodbye", language) == "Goodbye."
    assert tool_fillers(language) == en.TOOL_FILLERS


def test_missing_key_in_a_language_falls_back_to_english(monkeypatch):
    monkeypatch.delitem(hi.STRINGS, "fallback_llm_error")
    assert t("fallback_llm_error", "hi") == en.STRINGS["fallback_llm_error"]


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        t("no_such_key", "en")


def test_english_pipeline_strings_are_byte_identical():
    assert pipeline._FALLBACK_GOODBYE == "Goodbye."
    assert pipeline._FIRST_TURN_FILLER == "Mm-hmm, one moment."
    assert pipeline._MAX_DURATION_GOODBYE == (
        "We're at the time limit for this call now. Thanks for calling — goodbye."
    )
    assert pipeline._FALLBACK_LLM_ERROR == (
        "Sorry, I'm having a little trouble right now. Could you say that again?"
    )
    assert pipeline._TRANSFER_FAILED_FALLBACK == "I'm sorry, I couldn't connect you to an agent right now."
    assert _TOOL_FILLERS[0] == ("One moment.", 1.0)


def test_filler_selector_uses_the_language_pool():
    phrases = {p for p, _ in hi.TOOL_FILLERS}
    for _ in range(20):
        assert FillerSelector().select_tool_filler("t", None, 1500, language="hi") in phrases
    assert FillerSelector().select_tool_filler("t", None, 1000) in {p for p, _ in en.TOOL_FILLERS}
