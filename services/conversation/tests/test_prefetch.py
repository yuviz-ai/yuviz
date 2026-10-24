"""The pre-retrieval gate and settings: pure functions, no providers."""

from __future__ import annotations

import pytest

from services.conversation import prefetch


@pytest.mark.parametrize("text", [
    "What is your refund policy?",
    "how much does a three BHK cost",
    "Tell me about the payment plans",
    "when will I get possession",
    "Do you have parking for visitors",
    "Is there a loan facility",
    "आपका पता क्या है",
    "पज़ेशन कब मिलेगा",
    "kitna charge lagega yahan",
    "Greenfield Heights ka price kya hai",
])
def test_questions_and_topics_search(text):
    assert prefetch.wants_knowledge(text)


@pytest.mark.parametrize("text", [
    "My name is Rahul Sharma",
    "Yes please book it",
    "yes",
    "okay thank you",
    "I would like a visit on Saturday morning",
    "Monday at ten is fine",
    "हाँ ठीक है",
    "मेरा नाम अमित वर्मा है",
    "",
    "hello",
    "  ",
])
def test_booking_names_and_acknowledgements_do_not(text):
    assert not prefetch.wants_knowledge(text)


def test_two_word_utterances_never_search_even_with_a_trigger_word():
    assert not prefetch.wants_knowledge("what price")      # too short to be a reliable question


def test_settings_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("VOICEAI_PRE_RETRIEVAL", raising=False)
    monkeypatch.delenv("VOICEAI_PRE_RETRIEVAL_WAIT_MS", raising=False)
    assert prefetch.enabled() and prefetch.wait_s() == 0.25
    for off in ("0", "false", "OFF", "no"):
        assert not prefetch.enabled({"VOICEAI_PRE_RETRIEVAL": off})
    monkeypatch.setenv("VOICEAI_PRE_RETRIEVAL_WAIT_MS", "80")
    assert prefetch.wait_s() == 0.08
    monkeypatch.setenv("VOICEAI_PRE_RETRIEVAL_WAIT_MS", "garbage")
    assert prefetch.wait_s() == 0.25


def test_reference_block_wraps_caps_and_names_the_language():
    block = prefetch.reference_block(["Price is 1.62 crore.", "Possession is December 2027."], 0, answer_in="Hindi")
    assert block.startswith("(Not from the caller") and block.endswith(")")
    assert block.count("<retrieved_passage>") == 2 and "Answer in Hindi." in block
    many = prefetch.reference_block([f"passage number {i}" for i in range(10)], 0)
    assert many.count("<retrieved_passage>") == prefetch.MAX_PASSAGES
    assert prefetch.reference_block([], 0) is None
    assert "weak" not in prefetch.reference_block(["Price is 1.62 crore."], 0)
    assert "These matches are weak" in prefetch.reference_block(["Price is 1.62 crore."], 0, low_confidence=True)
