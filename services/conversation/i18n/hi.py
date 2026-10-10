"""Hindi spoken system strings (Devanagari, so every multilingual TTS reads them natively).
Phrased gender-neutrally: the agent's voice may be either."""

STRINGS: dict[str, str] = {
    "fallback_goodbye": "धन्यवाद, नमस्ते।",
    "max_duration_goodbye": "इस कॉल का समय पूरा हो गया है। कॉल करने के लिए धन्यवाद, नमस्ते।",
    "fallback_llm_error": "माफ़ कीजिए, अभी थोड़ी दिक्कत हो रही है। क्या आप फिर से बता सकते हैं?",
    "first_turn_filler": "जी, एक पल।",
    "transfer_failed_fallback": "माफ़ कीजिए, अभी आपको किसी एजेंट से जोड़ना संभव नहीं हो पाया।",
    "booking_fabrication_transfer_announcement": (
        "आपकी बुकिंग सही से हुई है, यह पक्का करने के लिए टीम के एक सदस्य से जाँच करवाई जा रही है — एक पल।"
    ),
}

TOOL_FILLERS: tuple[tuple[str, float], ...] = (
    ("एक पल।", 1.0),
    ("बस एक सेकंड।", 1.0),
    ("जी, अभी देखते हैं।", 1.5),
    ("एक मिनट दीजिए।", 1.5),
    ("ज़रूर, अभी इसे देखते हैं।", 2.0),
    ("एक पल रुकिए, अभी इसे पूरा करते हैं।", 2.4),
)
