"""LLMAdapter: tool-calling support without changing ILLM.generate().

Providers may implement IToolAwareLLM.generate_with_tools(); otherwise plain generate() is wrapped in TokenEvents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol, runtime_checkable

from ..providers.interfaces import ChatMessage


class TurnEvent:
    """Marker base — see TokenEvent/ToolCallEvent."""


@dataclass(frozen=True)
class TokenEvent(TurnEvent):
    text: str


@dataclass(frozen=True)
class ToolCallEvent(TurnEvent):
    tool_call_id: str
    tool_name:    str
    arguments:    dict[str, Any] = field(default_factory=dict)
    # Opaque per-provider passthrough (e.g. Gemini's thoughtSignature) that must be echoed back
    # verbatim when replayed into history; only the provider that set it reads it.
    provider_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolCallStartedEvent(TurnEvent):
    """Yielded just before a tool executes so the caller can speak a filler instead of dead air."""
    tool_name: str


@dataclass(frozen=True)
class DeterministicSpokenEvent(TurnEvent):
    """Text spoken verbatim with no LLM discretion, so a real tool success can't be confused with a hallucinated one."""
    text: str
    # The real confirmed slot, not just a "booked at some point" flag (see ToolResult.confirmed_datetime).
    confirmed_datetime: str | None = None


@dataclass(frozen=True)
class LocalToolCompletedEvent(TurnEvent):
    """Local tool finished; pipeline absorbs it (bridging speech comes later)."""
    tool_name: str


@runtime_checkable
class IToolAwareLLM(Protocol):
    """Optional companion to ILLM. A provider class implements this
    ADDITIONALLY to ILLM, never instead of it — see module docstring."""

    def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        ...


class LLMAdapter:
    """Wraps one ILLM; generate() always yields TurnEvents, whether or not the provider supports tools."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def generate(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        if schemas and isinstance(self._llm, IToolAwareLLM):
            async for event in self._llm.generate_with_tools(messages, schemas, tool_choice=tool_choice):
                yield event
            return

        # No tools this turn, or no provider tool support: wrap plain generate() uniformly.
        async for token in self._llm.generate(messages):
            yield TokenEvent(text=token)
