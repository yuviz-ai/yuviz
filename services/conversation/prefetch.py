"""Pre-retrieval: search the agent's knowledge base while the turn is still being set up, so the model answers a
knowledge question in ONE pass instead of calling `search_knowledge` and then being called again with the result.

This module holds the pure parts: the settings, the gate that decides whether a transcript is worth a search, and the
wording of the reference block handed to the model. The pipeline owns the task and the deadline (pipeline.py)."""

from __future__ import annotations

import os
import re

from .pipeline_config import env_ms
from .tools.envelope import UNTRUSTED_NOTICE, cap_passages, wrap_untrusted

# At most this many passages and this much text go to the model: the first token waits on every token in the prompt.
MAX_PASSAGES = 3
MAX_WORDS = 450
MAX_CHARS = 2400
_WAIT_MS_DEFAULT = 250


def enabled(environ: dict[str, str] | None = None) -> bool:
    """VOICEAI_PRE_RETRIEVAL (default on; 0/false/off/no disables). Only agents with a knowledge base ever search."""
    raw = (environ if environ is not None else os.environ).get("VOICEAI_PRE_RETRIEVAL", "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def wait_s() -> float:
    """The longest the turn waits for the search before starting the model without it
    (VOICEAI_PRE_RETRIEVAL_WAIT_MS, default 250; 0 starts the model at once and uses a finished search only)."""
    return env_ms("VOICEAI_PRE_RETRIEVAL_WAIT_MS", _WAIT_MS_DEFAULT) / 1000


# ── the gate ──────────────────────────────────────────────────────────────

_WORD = re.compile(r"[\wऀ-ॿ]+")

# Replies, greetings and agreements never need a search.
_ACKNOWLEDGEMENTS = frozenset({
    "yes", "no", "ok", "okay", "yeah", "yep", "sure", "fine", "thanks", "thank", "you", "hello", "hi", "hey", "bye",
    "goodbye", "please", "right", "correct", "done", "alright", "haan", "han", "ji", "nahi", "nahin", "theek", "hai",
    "accha", "acha", "shukriya", "dhanyavad", "हाँ", "हां", "जी", "नहीं", "ठीक", "है", "अच्छा", "धन्यवाद", "शुक्रिया",
})

# Words that mark a question or a topic the business documents answer (prices, places, policies). Booking words
# (book, visit, cancel, tomorrow, morning, time) are deliberately absent: those turns use the booking tools.
_TRIGGERS = frozenset({
    # English question words and topics
    "what", "whats", "when", "where", "which", "who", "why", "how", "price", "prices", "pricing", "cost", "costs",
    "fee", "fees", "charge", "charges", "rate", "rates", "location", "address", "timing", "timings", "hours", "parking",
    "policy", "policies", "loan", "loans", "emi", "payment", "payments", "discount", "discounts", "offer", "offers",
    "amenities", "facilities", "possession", "documents", "brokerage", "tell", "explain", "details", "information",
    "info", "refund", "warranty", "insurance",
    # Hinglish
    "kya", "kab", "kahan", "kaun", "kaunsa", "kitna", "kitne", "kitni", "kaise", "kyun", "kyon", "keemat", "daam",
    "bataiye", "batao", "bataye", "jankari",
    # Hindi
    "क्या", "कब", "कहाँ", "कहां", "कौन", "कौनसा", "कौन-सा", "कितना", "कितने", "कितनी", "कैसे", "क्यों", "कीमत", "दाम",
    "फीस", "चार्ज", "शुल्क", "पता", "लोकेशन", "टाइमिंग", "लोन", "किस्त", "भुगतान", "छूट", "डिस्काउंट", "सुविधा",
    "सुविधाएं", "पज़ेशन", "कब्ज़ा", "दस्तावेज़", "दस्तावेज", "जानकारी", "बताइए", "बताओ", "बताएं", "बताइये",
})
_PHRASES = ("is there", "are there", "does it", "do you have", "do you offer", "do you accept", "tell me about")


def wants_knowledge(text: str) -> bool:
    """True when the caller's words look like a question or topic the knowledge base answers. Conservative on
    purpose: a missed question still works (the model calls search_knowledge itself, one pass slower), while a
    needless search spends a little load and puts unrelated passages in front of the model."""
    folded = (text or "").casefold().strip()
    words = _WORD.findall(folded)
    if len(words) < 3 or all(w in _ACKNOWLEDGEMENTS for w in words):
        return False
    if "?" in folded or "？" in folded:
        return True
    return any(w in _TRIGGERS for w in words) or any(p in folded for p in _PHRASES)


# ── what the model is given ───────────────────────────────────────────────

def reference_block(
    passages: list[str], always_included: int, *, answer_in: str | None = None, low_confidence: bool = False,
) -> str | None:
    """The note that carries the retrieved passages, or None when nothing is left after capping.
    Passages are wrapped as untrusted data exactly like a search_knowledge result. `low_confidence` (the search
    flagged its best match as weak, as keyword-only evidence always is) tells the model to use them only when they
    clearly answer the question, as the search_knowledge result does."""
    kept, _truncated = cap_passages(passages[:MAX_PASSAGES + always_included], always_included, MAX_WORDS, MAX_CHARS)
    if not kept:
        return None
    body = "\n".join(wrap_untrusted(text) for text in kept)
    language = f" Answer in {answer_in}." if answer_in else ""
    weak = " These matches are weak: use them only if they clearly answer the question." if low_confidence else ""
    return (
        "(Not from the caller: reference passages already retrieved for their latest message, from the business's "
        f"uploaded documents. {UNTRUSTED_NOTICE} If they answer the question, answer from them directly and do not "
        f"call search_knowledge for this question; if they do not, you may call search_knowledge once.{weak}{language}\n"
        f"{body})"
    )
