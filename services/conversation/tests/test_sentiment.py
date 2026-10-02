"""SentimentScorer: parsing, transcript rendering, and failure modes (no DB, no real model)."""

from __future__ import annotations

import asyncio

import pytest

from ..sentiment import SentimentScorer, Turn


class FakeLLM:
    """Minimal ILLM: yields a canned response, one token at a time so the
    scorer's own token accumulation is exercised rather than bypassed."""

    def __init__(self, response: str = "", *, raises: Exception | None = None,
                 delay_s: float = 0.0) -> None:
        self.response = response
        self.raises = raises
        self.delay_s = delay_s
        self.messages = None

    async def generate(self, messages):
        self.messages = messages
        if self.raises is not None:
            raise self.raises
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        for chunk in self.response:
            yield chunk


_TURNS = [Turn(caller_text="I've told you this twice already", ai_response="Sorry, could you repeat that?")]


async def test_parses_a_well_formed_response():
    llm = FakeLLM('{"label": "frustrated", "reason": "caller repeated themselves twice"}')
    result = await SentimentScorer(llm).score(_TURNS)
    assert result is not None
    assert result.label == "frustrated"
    assert result.reason == "caller repeated themselves twice"


async def test_tolerates_fenced_and_prefixed_json():
    """JSON wrapped in prose or code fences still parses."""
    llm = FakeLLM('Here is my analysis:\n```json\n{"label": "positive", "reason": "caller thanked the agent"}\n```')
    result = await SentimentScorer(llm).score(_TURNS)
    assert result is not None
    assert result.label == "positive"


@pytest.mark.parametrize("response", [
    "",                                        # model said nothing
    "the caller sounded upset",                # prose, no JSON at all
    '{"label": "frustrated", "reason":}',      # malformed JSON
    '{"label": "furious", "reason": "x"}',     # label outside the contract
    '{"reason": "no label at all"}',
    '["frustrated"]',                          # JSON, but not an object
])
async def test_unusable_responses_score_as_none(response):
    """Unusable responses yield None, never a fabricated 'neutral'."""
    assert await SentimentScorer(FakeLLM(response)).score(_TURNS) is None


async def test_llm_failure_is_swallowed():
    llm = FakeLLM(raises=RuntimeError("connection refused"))
    assert await SentimentScorer(llm).score(_TURNS) is None


async def test_timeout_is_bounded():
    llm = FakeLLM('{"label": "positive", "reason": "x"}', delay_s=5.0)
    assert await SentimentScorer(llm, timeout_s=0.05).score(_TURNS) is None


async def test_no_caller_speech_is_never_scored():
    """A call where only the agent spoke has no caller mood to report —
    scoring it would invent one from the agent talking to itself."""
    turns = [
        Turn(caller_text=None, ai_response="Hello, how can I help?"),
        Turn(caller_text="   ", ai_response="Are you still there?"),
    ]
    llm = FakeLLM('{"label": "neutral", "reason": "quiet"}')
    assert await SentimentScorer(llm).score(turns) is None
    assert llm.messages is None, "the model should never have been called"


async def test_system_prompt_is_injected_so_the_agent_prompt_is_overridden():
    """A system-role message is injected so the scorer doesn't inherit the agent's persona."""
    llm = FakeLLM('{"label": "neutral", "reason": "routine"}')
    await SentimentScorer(llm).score(_TURNS)
    assert llm.messages[0].role == "system"
    assert "JSON" in llm.messages[0].content
    assert llm.messages[1].role == "user"
    assert "I've told you this twice already" in llm.messages[1].content


async def test_long_transcripts_keep_the_ending():
    """The middle is dropped, not the tail — how a call ENDED is the single
    most informative part for sentiment, so it must survive truncation."""
    turns = [Turn(caller_text=f"turn {i}", ai_response="ok") for i in range(100)]
    llm = FakeLLM('{"label": "neutral", "reason": "x"}')
    await SentimentScorer(llm, max_turns=10).score(turns)

    rendered = llm.messages[1].content
    assert "turn 0" in rendered
    assert "turn 99" in rendered
    assert "turn 50" not in rendered
    assert "90 turns omitted" in rendered


async def test_overlong_reason_is_truncated():
    llm = FakeLLM('{"label": "negative", "reason": "' + "x" * 500 + '"}')
    result = await SentimentScorer(llm).score(_TURNS)
    assert result is not None
    assert len(result.reason) <= 160
