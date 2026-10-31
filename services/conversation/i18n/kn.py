"""Kannada spoken system strings (Kannada script). Machine-drafted: have a native speaker review before production."""

STRINGS: dict[str, str] = {
    "fallback_goodbye": "ಧನ್ಯವಾದಗಳು, ನಮಸ್ಕಾರ.",
    "max_duration_goodbye": "ಈ ಕರೆಯ ಸಮಯ ಮುಗಿದಿದೆ. ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು, ನಮಸ್ಕಾರ.",
    "fallback_llm_error": "ಕ್ಷಮಿಸಿ, ಈಗ ಸ್ವಲ್ಪ ತೊಂದರೆ ಆಗುತ್ತಿದೆ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳುತ್ತೀರಾ?",
    "end_call_check": "ಇನ್ನೇನಾದರೂ ಸಹಾಯ ಬೇಕೇ?",
    "transfer_failed_fallback": "ಕ್ಷಮಿಸಿ, ಈಗ ನಿಮ್ಮನ್ನು ಪ್ರತಿನಿಧಿಯೊಂದಿಗೆ ಸಂಪರ್ಕಿಸಲು ಆಗಲಿಲ್ಲ.",
    "callflow_dial_refused": "ಈಗ ಈ ಕರೆಯನ್ನು ವರ್ಗಾಯಿಸಲು ಸಾಧ್ಯವಿಲ್ಲ. ದಯವಿಟ್ಟು ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ. ನಮಸ್ಕಾರ.",
    "booking_fabrication_transfer_announcement": (
        "ನಿಮ್ಮ ಬುಕಿಂಗ್ ಸರಿಯಾಗಿ ಆಗಿದೆಯೇ ಎಂದು ಖಚಿತಪಡಿಸಲು ನಮ್ಮ ತಂಡದ ಸದಸ್ಯರು ಪರಿಶೀಲಿಸುತ್ತಿದ್ದಾರೆ — ಒಂದು ಕ್ಷಣ."
    ),
}

TOOL_FILLERS: tuple[tuple[str, float], ...] = (
    ("ಒಂದು ಕ್ಷಣ.", 1.0),
    ("ಒಂದು ಸೆಕೆಂಡ್.", 1.0),
    ("ಸರಿ, ಈಗ ನೋಡೋಣ.", 1.5),
    ("ಸ್ವಲ್ಪ ಕಾಯಿರಿ.", 1.5),
    ("ಖಂಡಿತ, ಈಗಲೇ ನೋಡೋಣ.", 2.0),
    ("ಒಂದು ಕ್ಷಣ ಕಾಯಿರಿ, ಈಗಲೇ ಮುಗಿಸೋಣ.", 2.4),
)

FIRST_TURN_FILLERS: tuple[tuple[str, float], ...] = (
    ("ಸರಿ, ಒಂದು ಕ್ಷಣ.", 1.0),
    ("ಸರಿ, ಒಂದು ಸೆಕೆಂಡ್.", 1.0),
    ("ಸರಿ, ಸ್ವಲ್ಪ ಕಾಯಿರಿ.", 1.5),
    ("ಸರಿ, ಒಂದು ಕ್ಷಣ ಕೊಡಿ.", 1.5),
    ("ಸರಿ, ದಯವಿಟ್ಟು ಒಂದು ಕ್ಷಣ ಕಾಯಿರಿ.", 2.0),
    ("ಸರಿ, ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಕಾಯಿರಿ.", 2.4),
)

# "ನಮಸ್ಕಾರ" opens calls too, so it is a farewell only, not a closure.
CLOSURE_PHRASES: tuple[str, ...] = ("ಅಷ್ಟೇ", "ಬೇರೆ ಏನೂ ಇಲ್ಲ", "ಧನ್ಯವಾದಗಳು", "ಬೈ", "ಸಾಕು")
FAREWELL_PHRASES: tuple[str, ...] = ("ನಮಸ್ಕಾರ", "ಮತ್ತೆ ಸಿಗೋಣ", "ಶುಭ ದಿನ", "ಹುಷಾರಾಗಿರಿ", "ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು")
