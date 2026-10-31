"""Marathi spoken system strings (Devanagari). Machine-drafted: have a native speaker review before production.
Phrased without first-person gendered verbs: the agent's voice may be either."""

STRINGS: dict[str, str] = {
    "fallback_goodbye": "धन्यवाद, नमस्कार.",
    "max_duration_goodbye": "या कॉलची वेळ संपली आहे. कॉल केल्याबद्दल धन्यवाद, नमस्कार.",
    "fallback_llm_error": "माफ करा, सध्या थोडी अडचण येत आहे. कृपया पुन्हा सांगाल का?",
    "end_call_check": "आणखी काही मदत हवी आहे का?",
    "transfer_failed_fallback": "माफ करा, सध्या तुम्हाला प्रतिनिधीशी जोडता आले नाही.",
    "callflow_dial_refused": "सध्या हा कॉल ट्रान्सफर करता येत नाही. कृपया नंतर पुन्हा प्रयत्न करा. नमस्कार.",
    "booking_fabrication_transfer_announcement": (
        "तुमचे बुकिंग बरोबर झाले आहे याची खात्री करण्यासाठी टीममधील एक सदस्य तपासत आहेत — एक क्षण."
    ),
}

TOOL_FILLERS: tuple[tuple[str, float], ...] = (
    ("एक क्षण.", 1.0),
    ("एक सेकंद.", 1.0),
    ("हो, आत्ता पाहूया.", 1.5),
    ("थोडा वेळ द्या.", 1.5),
    ("नक्की, आत्ताच तपासूया.", 2.0),
    ("एक क्षण थांबा, हे आत्ताच पूर्ण करूया.", 2.4),
)

FIRST_TURN_FILLERS: tuple[tuple[str, float], ...] = (
    ("हो, एक क्षण.", 1.0),
    ("हो, एक सेकंद.", 1.0),
    ("हो, जरा थांबा.", 1.5),
    ("हो, एक क्षण द्या.", 1.5),
    ("हो, कृपया एक क्षण थांबा.", 2.0),
    ("हो, कृपया जरा एक क्षण थांबा.", 2.4),
)

# "नमस्कार" opens calls too, so it is a farewell only, not a closure.
CLOSURE_PHRASES: tuple[str, ...] = ("एवढंच", "बाकी काही नाही", "धन्यवाद", "बाय", "बस झालं")
FAREWELL_PHRASES: tuple[str, ...] = ("नमस्कार", "पुन्हा भेटू", "दिवस छान जावो", "काळजी घ्या", "कॉल केल्याबद्दल धन्यवाद")
