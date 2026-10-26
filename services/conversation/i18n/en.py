"""English spoken system strings — the fallback for every other language."""

STRINGS: dict[str, str] = {
    # Spoken when END_CALL comes with no text: the servicer drops EndCall unless some TTS was sent.
    "fallback_goodbye": "Goodbye.",
    # Spoken when max_call_duration_s is exceeded; not farewell_message, which implies a natural end.
    "max_duration_goodbye": "We're at the time limit for this call now. Thanks for calling — goodbye.",
    # Spoken when the LLM stream raises mid-turn, so the caller doesn't hear dead air.
    "fallback_llm_error": "Sorry, I'm having a little trouble right now. Could you say that again?",
    # Spoken when a lone [[END_CALL]] is rejected (the caller did not say goodbye): the call stays open.
    "end_call_check": "Is there anything else I can help you with?",
    # Used if the LLM produces no apology text after a failed transfer.
    "transfer_failed_fallback": "I'm sorry, I couldn't connect you to an agent right now.",
    # Spoken before the hangup when a call-flow Dial node's destination is refused by the transfer policy.
    "callflow_dial_refused": "I can't transfer this call right now. Please try again later. Goodbye.",
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

# Neutral opener spoken before the LLM has decided anything, so no lookup or task wording.
FIRST_TURN_FILLERS: tuple[tuple[str, float], ...] = (
    ("Okay, one moment.", 1.0),
    ("Alright, just a second.", 1.0),
    ("Okay, one moment please.", 1.5),
    ("Alright, one moment please.", 1.5),
    ("Alright, just give me a moment please.", 2.0),
    ("Okay, just give me one moment here, please.", 2.4),
)

# Phrases that mean the caller is wrapping up, and phrases that sound like the agent signing off.
CLOSURE_PHRASES: tuple[str, ...] = (
    "bye",
    "goodbye",
    "that's all",
    "thats all",
    "that is all",
    "that's it",
    "no more questions",
    "nothing else",
    "i'm done",
    "thanks",
    "thank you",
    "okay thanks",
)
FAREWELL_PHRASES: tuple[str, ...] = (
    "goodbye",
    "bye",
    "take care",
    "have a great day",
    "have a nice day",
    "have a good day",
    "thanks for calling",
    "thank you for calling",
    "have a wonderful day",
    "have a lovely day",
    "talk to you soon",
    "see you",
)
