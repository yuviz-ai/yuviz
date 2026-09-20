"""
SentimentScorer — how the CALLER sounded, scored once per call from the
finished transcript.

Runs inside TranscriptBuilder's end_call() background write (see
transcript_builder.py), never on the live turn path: by the time this is
called the caller has already hung up, so an LLM round-trip here costs the
conversation nothing. Everything about this module is best-effort — a
timeout, an unreachable model, or a model that ignores the output contract
all resolve to None, which the schema records as "never scored" and the
Admin UI renders as "—". A call is never left unfinalized because scoring
failed; _end_call() writes ended_at/duration/turn_count first and only then
attempts this.

Deliberately NOT per-turn: one score over the whole conversation is the
question the Call Log actually asks ("did this caller leave happy?"), and
it costs one LLM call per call instead of one per turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from .providers.interfaces import ChatMessage

log = logging.getLogger(__name__)

# Mirrors database/schema.sql's calls_sentiment_check. 'frustrated' is
# separate from 'negative' on purpose — see that constraint's comment for
# why collapsing them loses the actionable half.
LABELS = ("positive", "neutral", "negative", "frustrated")

_SYSTEM_PROMPT = """You analyse finished customer-service phone call transcripts.

Judge ONLY the caller's experience — never the agent's politeness, and never
whether you personally agree with the outcome.

Reply with a single JSON object and nothing else:
{"label": "...", "reason": "..."}

Choose exactly one label:
  positive   - the caller got what they wanted and showed it
  neutral    - a routine exchange; no strong feeling either way
  negative   - the caller was unhappy with the ANSWER or outcome, but the
               conversation itself worked
  frustrated - the caller struggled with the AGENT: repeating themselves,
               being misunderstood, asking for a human, or giving up

The reason must describe THIS call, in under 100 characters. Cite what the
caller actually said or did. Never restate the label definitions above —
they describe categories, not this conversation.

Example of a good reply:
{"label": "frustrated", "reason": "asked for the same refund three times before the agent understood"}

Example of a BAD reply (this copies the definition instead of the call):
{"label": "frustrated", "reason": "struggled with the agent, repeating themselves or giving up"}"""

# The model is asked for bare JSON, but small local models habitually wrap it
# in ```json fences or prepend a sentence. Pull the first balanced-looking
# object out rather than failing the whole score over formatting.
_JSON_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)

_MAX_REASON_CHARS = 160


@dataclass(frozen=True)
class SentimentResult:
    label: str
    reason: str


@dataclass(frozen=True)
class Turn:
    """One round-trip, matching transcript_entries' own row shape."""
    caller_text: str | None
    ai_response: str | None


class SentimentScorer:
    """
    llm       — any ILLM (services/conversation/providers/interfaces.py). Only
                plain generate() is used, with an explicit system-role message
                so the agent's own conversational system prompt is overridden
                rather than appended to (see build_chat_messages()).
    max_turns — cap on transcript turns sent. A 200-turn call would otherwise
                build a prompt large enough to be slow and to push the output
                contract out of a small model's attention; the first and last
                turns carry the arc, so the middle is dropped, not the end.
    timeout_s — hard ceiling on the whole generation. Nothing waits on this,
                but the write chain for this session is held until it returns,
                so it cannot be unbounded.
    """

    def __init__(
        self,
        llm,
        *,
        max_turns: int = 40,
        timeout_s: float = 20.0,
    ) -> None:
        self._llm = llm
        self._max_turns = max_turns
        self._timeout_s = timeout_s

    async def score(self, turns: list[Turn]) -> SentimentResult | None:
        """None on anything less than a confident, valid reading."""
        transcript = self._render(turns)
        if transcript is None:
            return None
        try:
            async with asyncio.timeout(self._timeout_s):
                raw = await self._generate(transcript)
        except asyncio.TimeoutError:
            log.warning("SentimentScorer: timed out after %.1fs", self._timeout_s)
            return None
        except Exception:
            log.exception("SentimentScorer: generation failed")
            return None
        return self._parse(raw)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _render(self, turns: list[Turn]) -> str | None:
        """None when there is nothing a human could score either.

        Turns where the caller said nothing are dropped before the length
        check: a call can hold several agent-only turns (a greeting, then a
        timeout) and scoring the agent talking to itself would report a
        caller mood that was never expressed."""
        spoken = [t for t in turns if (t.caller_text or "").strip()]
        if not spoken:
            return None

        if len(spoken) > self._max_turns:
            head = self._max_turns // 2
            tail = self._max_turns - head
            kept = spoken[:head] + spoken[-tail:]
            elision = len(spoken) - self._max_turns
        else:
            kept = spoken
            elision = 0

        lines: list[str] = []
        for i, turn in enumerate(kept):
            if elision and i == self._max_turns // 2:
                lines.append(f"[... {elision} turns omitted ...]")
            lines.append(f"Caller: {(turn.caller_text or '').strip()}")
            agent = (turn.ai_response or "").strip()
            lines.append(f"Agent: {agent}" if agent else "Agent: (no reply)")
        return "\n".join(lines)

    async def _generate(self, transcript: str) -> str:
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=f"Transcript:\n\n{transcript}"),
        ]
        chunks: list[str] = []
        async for token in self._llm.generate(messages):
            chunks.append(token)
        return "".join(chunks)

    def _parse(self, raw: str) -> SentimentResult | None:
        match = _JSON_OBJECT.search(raw)
        if match is None:
            log.warning("SentimentScorer: no JSON object in response=%r", raw[:200])
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            log.warning("SentimentScorer: malformed JSON=%r", match.group(0)[:200])
            return None
        if not isinstance(data, dict):
            return None

        label = str(data.get("label", "")).strip().lower()
        if label not in LABELS:
            # A label outside the contract is a failed read, not something to
            # coerce to 'neutral' — the schema's NULL says "never scored",
            # which is the truth here.
            log.warning("SentimentScorer: unknown label=%r", label)
            return None

        reason = " ".join(str(data.get("reason", "")).split()).rstrip(".")
        if len(reason) > _MAX_REASON_CHARS:
            reason = reason[: _MAX_REASON_CHARS - 1].rstrip() + "…"
        return SentimentResult(label=label, reason=reason)
