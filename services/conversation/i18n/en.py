"""English spoken system strings — the fallback for every other language."""

STRINGS: dict[str, str] = {
    # Spoken when END_CALL comes with no text: the servicer drops EndCall unless some TTS was sent.
    "fallback_goodbye": "Goodbye.",
    # Spoken when max_call_duration_s is exceeded; not farewell_message, which implies a natural end.
    "max_duration_goodbye": "We're at the time limit for this call now. Thanks for calling — goodbye.",
    # Spoken when the LLM stream raises mid-turn, so the caller doesn't hear dead air.
    "fallback_llm_error": "Sorry, I'm having a little trouble right now. Could you say that again?",
    # Masks turn-1 LLM latency; generic so it fits any first utterance.
    "first_turn_filler": "Mm-hmm, one moment.",
    # Used if the LLM produces no apology text after a failed transfer.
    "transfer_failed_fallback": "I'm sorry, I couldn't connect you to an agent right now.",
    # Replaces transfer_announcement for a fabrication-triggered transfer.
    "booking_fabrication_transfer_announcement": (
        "Let me just double-check that booking with a team member to make sure "
        "it's set up correctly — one moment."
    ),
}

# (phrase, approx spoken seconds); lengths are estimates, not measured.
TOOL_FILLERS: tuple[tuple[str, float], ...] = (
    ("One moment.", 1.0),
    ("Just a second.", 1.0),
    ("Let me check on that.", 1.5),
    ("Give me a moment.", 1.5),
    ("Sure, let me look into that.", 2.0),
    ("One moment while I take care of that.", 2.4),
)
