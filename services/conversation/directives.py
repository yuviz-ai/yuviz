"""Inline LLM control tokens such as [[END_CALL]] and [[TRANSFER type="warm" ...]].

StreamBuffer holds back unterminated "[[...]]" so a tag (whose attrs may contain
sentence punctuation) is never split and spoken; DirectiveParser strips complete tags.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Union

_TAG_RE  = re.compile(r'\[\[(?P<name>[A-Z_]+)(?P<attrs>[^\]]*)\]\]')
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# TTS speaks markdown chars literally. Blanket strip (not paired regex) because
# markers can be split across streamed chunks.
_MARKDOWN_CHARS_RE = re.compile(r"[*_`#]")


def strip_markdown_chars(text: str) -> str:
    """Strip markdown chars from any text bound for TTS, not just LLM output. Idempotent."""
    return _MARKDOWN_CHARS_RE.sub("", text)


class TransferType(str, Enum):
    """Mirrors agents.transfer_type's CHECK constraint; keep both in sync.
    str(TransferType.WARM) is not "warm" — use `.value` for the plain string."""
    WARM = "warm"
    COLD = "cold"
    NONE = "none"


def coerce_transfer_type(raw: str) -> TransferType:
    """Parse a transfer type, degrading unknown values to NONE instead of raising."""
    try:
        return TransferType(raw)
    except ValueError:
        return TransferType.NONE


@dataclass(frozen=True)
class EndCallDirective:
    pass


@dataclass(frozen=True)
class TransferDirective:
    transfer_type: TransferType
    destination:   str
    reason:        str


@dataclass(frozen=True)
class UnknownDirective:
    """A directive tag with no typed class yet (kind + raw attrs)."""
    kind:  str
    attrs: dict[str, str] = field(default_factory=dict)


Directive = Union[EndCallDirective, TransferDirective, UnknownDirective]


@dataclass(frozen=True)
class TransferRequest:
    """A transfer to execute, from a directive or an escalation trigger; servicer.py
    sends it to the gateway after the acknowledgment audio finishes."""
    session_id:    str
    tenant_id:     str
    call_id:       str
    transfer_type: TransferType
    destination:   str
    reason:        str
    trigger:       str = "llm_directive"   # or "escalation_threshold"
    # Per-attempt correlation id echoed back on Transfer* events (observability only).
    transfer_id:   str = field(default_factory=lambda: uuid.uuid4().hex)
    # Warm transfer agent-leg caller ID, already resolved; "" means the caller's own ANI.
    caller_id:     str = ""
    # Raw agent.transfer_waiting_experience, resolved by the gateway; "" means announcement_moh.
    waiting_experience: str = ""


@dataclass(frozen=True)
class DirectiveResult:
    """Text with complete tags stripped (TTS-safe), plus the directives found, in order."""
    clean_text: str
    directives: list[Directive] = field(default_factory=list)


class StreamBuffer:
    """Buffers streamed text, holding back an unterminated "[[" tail until it closes."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> str:
        """Return text now safe to parse; never a partial tag."""
        self._pending += chunk
        last_open = self._pending.rfind("[[")
        if last_open != -1 and "]]" not in self._pending[last_open:]:
            safe, self._pending = self._pending[:last_open], self._pending[last_open:]
        else:
            safe, self._pending = self._pending, ""
        return safe

    def flush(self) -> str:
        """End of stream: an unclosed "[[" was prose, so return it as text."""
        remainder, self._pending = self._pending, ""
        return remainder


class DirectiveParser:
    """Pure str -> DirectiveResult parsing; input never contains a partial tag."""

    @staticmethod
    def parse(text: str) -> DirectiveResult:
        directives: list[Directive] = []

        def _record(m: "re.Match[str]") -> str:
            directives.append(DirectiveParser._build(
                m.group("name"), dict(_ATTR_RE.findall(m.group("attrs"))),
            ))
            return ""

        clean_text = _TAG_RE.sub(_record, text)
        clean_text = strip_markdown_chars(clean_text)
        return DirectiveResult(clean_text=clean_text, directives=directives)

    @staticmethod
    def _build(name: str, attrs: dict[str, str]) -> Directive:
        if name == "END_CALL":
            return EndCallDirective()
        if name == "TRANSFER":
            return TransferDirective(
                transfer_type=coerce_transfer_type(attrs.get("type", "none")),
                destination=attrs.get("destination", ""),
                reason=attrs.get("reason", ""),
            )
        return UnknownDirective(kind=name, attrs=attrs)
