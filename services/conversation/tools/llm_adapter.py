"""LLMAdapter: tool-calling support without changing ILLM.generate().

Providers may implement IToolAwareLLM.generate_with_tools(); otherwise plain generate() is wrapped in TokenEvents.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator, Protocol, runtime_checkable

from ..providers.interfaces import ChatMessage

log = logging.getLogger(__name__)

# A text-form tool call is a few hundred bytes; past this a reply that began with "{" is treated as prose.
_MAX_HELD_JSON_CHARS = 2048
_FENCE = "```json"
_FENCE_OPEN = re.compile(r"```(?:json)?\s*", re.IGNORECASE)
_JSON_OBJECT_START = re.compile(r"\{\s*(\"|\})")
_TOOL_NAME_KEYS = ("name", "function", "tool_name", "tool")
# The start of a tool call written as text: never spoken, even when cut off or too long to parse.
_TOOL_CALL_START = re.compile(r'^\s*(?:```(?:json)?\s*)?\{\s*"(?:name|function|tool_name|tool)"\s*:', re.IGNORECASE)


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
    # True = a progress signal for a slow local tool (speak a filler), not a tool call to count: the
    # fabricated-booking guard must not be satisfied by a lookup that merely ran long.
    filler_only: bool = False


@dataclass(frozen=True)
class ToolCallFinishedEvent(TurnEvent):
    """Yielded once a tool has produced its result; `status` is the ToolStatus value."""
    tool_name: str
    status:    str


@dataclass(frozen=True)
class DeterministicSpokenEvent(TurnEvent):
    """Text spoken verbatim with no LLM discretion, so a real tool success can't be confused with a hallucinated one."""
    text: str
    # The real confirmed slot, not just a "booked at some point" flag (see ToolResult.confirmed_datetime).
    confirmed_datetime: str | None = None


@dataclass(frozen=True)
class LocalToolCompletedEvent(TurnEvent):
    """Local tool finished; pipeline absorbs it (bridging speech comes later). `status` is the ToolStatus value."""
    tool_name: str
    status:    str


@runtime_checkable
class IToolAwareLLM(Protocol):
    """Optional companion to ILLM. A provider class implements this
    ADDITIONALLY to ILLM, never instead of it — see module docstring."""

    def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        ...


def _text_tool_call(obj: Any) -> tuple[str, dict[str, Any]] | None:
    """(name, arguments) when obj names a tool and carries "parameters" or "arguments", optionally under "function"."""
    if isinstance(obj, dict) and isinstance(obj.get("function"), dict):
        obj = obj["function"]
    if not isinstance(obj, dict):
        return None
    name = next((obj[k] for k in _TOOL_NAME_KEYS if isinstance(obj.get(k), str)), None)
    arguments = obj.get("parameters", obj.get("arguments"))
    return (name, arguments) if name is not None and isinstance(arguments, dict) else None


def _match_tool_name(name: str, tool_names: set[str]) -> str | None:
    """The offered tool `name` refers to, tolerating mangling like `searchknowledge` for `search_knowledge`."""
    if name in tool_names:
        return name
    squash = lambda n: re.sub(r"[\s_-]", "", n).lower()  # noqa: E731
    matches = [t for t in tool_names if squash(t) == squash(name)]
    return matches[0] if len(matches) == 1 else None


def _object_end(text: str) -> int | None:
    """Index just past the object that starts text, matching braces outside strings; None if it is unterminated."""
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text):
        if in_string:
            in_string, escaped = not (ch == '"' and not escaped), ch == "\\" and not escaped
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _parse_segment(held: str) -> tuple[str, Any, str]:
    """Classify held reply text: ("wait", ...) = could still become a JSON object, ("prose", ...) = release it,
    ("json", obj, rest) = a complete object (obj None if it is not valid JSON) followed by `rest`."""
    body = held.lstrip()
    fenced = body[0] == "`"
    if fenced:
        if len(body) < len(_FENCE) and _FENCE.startswith(body.lower()):
            return "wait", None, ""
        opening = _FENCE_OPEN.match(body)
        if opening is None:
            return "prose", None, ""
        body = body[opening.end():]
        if not body:
            return "wait", None, ""
    if body[0] != "{" or (body[1:].strip() and not _JSON_OBJECT_START.match(body)):
        return "prose", None, ""
    end = _object_end(body)
    if end is None:
        return "wait", None, ""
    rest = body[end:]
    if fenced:
        rest = rest.lstrip().removeprefix("```")
    try:
        return "json", json.loads(body[:end]), rest
    except ValueError:
        return "json", None, rest


async def recover_text_tool_calls(
    events: AsyncIterator[TurnEvent], tool_names: set[str],
) -> AsyncGenerator[TurnEvent, None]:
    """Keep a tool call the model wrote as reply text from being spoken.

    A reply whose first non-blank text is "{" or a ```json fence is held back until the JSON is complete (or
    clearly is not one, or passes _MAX_HELD_JSON_CHARS). A call to an offered tool becomes a ToolCallEvent; a call
    to an unknown tool, or any call when none are offered, is dropped. Other JSON and all prose pass through
    unchanged; prose is never held past its first character."""
    held = ""
    prose = False
    dropping = False  # the rest of an over-long text tool call: still not speech
    async for event in events:
        if dropping and isinstance(event, TokenEvent):
            continue
        if prose or not isinstance(event, TokenEvent):
            if held.strip():
                log.warning("text_tool_call_dropped: incomplete JSON before a native event (%d chars)", len(held))
            held = ""
            yield event
            continue
        held += event.text
        while held.strip():
            kind, obj, rest = _parse_segment(held)
            if kind == "wait" and len(held) <= _MAX_HELD_JSON_CHARS:
                break
            shaped = _TOOL_CALL_START.match(held) is not None
            if kind == "wait" and shaped:
                log.warning("text_tool_call_dropped: too_long (%d chars)", len(held))
                held, dropping = "", True
                break
            call = _text_tool_call(obj)
            if kind != "json" or (call is None and not shaped):
                # Prose, JSON that is not a tool call, or JSON too long to keep waiting for.
                prose = True
                yield TokenEvent(text=held)
                held = ""
                break
            segment_len, held = len(held) - len(rest), rest  # whatever follows starts a new segment
            if call is None:
                named = obj.get("name") or obj.get("tool") or obj.get("tool_name") if isinstance(obj, dict) else None
                log.warning("text_tool_call_dropped: unparseable tool=%r len=%d", str(named)[:64] if named else None, segment_len)
                continue
            name = _match_tool_name(call[0], tool_names)
            if name is None:
                log.warning("text_tool_call_dropped: %s name=%r", "unknown_tool" if tool_names else "no_tools_offered",
                            call[0][:64])
                continue
            yield ToolCallEvent(tool_call_id=f"text_call_{uuid.uuid4().hex[:12]}", tool_name=name, arguments=call[1])
    if held.strip() and _TOOL_CALL_START.match(held):
        log.warning("text_tool_call_dropped: cut_off (%d chars)", len(held))
    elif held.strip("` \n"):  # a bare closing fence left after a fenced call is not speech
        yield TokenEvent(text=held)


class LLMAdapter:
    """Wraps one ILLM; generate() always yields TurnEvents, whether or not the provider supports tools."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def generate(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        if schemas and isinstance(self._llm, IToolAwareLLM):
            events = self._llm.generate_with_tools(messages, schemas, tool_choice=tool_choice)
        else:
            # No tools this turn, or no provider tool support: wrap plain generate() uniformly.
            events = self._tokens(messages)
        async for event in recover_text_tool_calls(events, {s["name"] for s in schemas or []}):
            yield event

    async def _tokens(self, messages: list[ChatMessage]) -> AsyncGenerator[TurnEvent, None]:
        async for token in self._llm.generate(messages):
            yield TokenEvent(text=token)
