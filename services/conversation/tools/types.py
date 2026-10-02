"""Core tool-execution types. Must stay importable on its own (no providers/ or pipeline imports).
ToolStatus is a small vocabulary shared by every tool; don't add per-tool statuses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolStatus(str, Enum):
    SUCCESS          = "success"
    FAILED           = "failed"
    TIMEOUT          = "timeout"
    CANCELLED        = "cancelled"
    UNAVAILABLE      = "unavailable"        # circuit breaker open — provider never even dialed
    INVALID_ARGUMENT = "invalid_argument"   # schema- or business-rule-level; see missing_fields
    RATE_LIMITED     = "rate_limited"


@dataclass(frozen=True)
class ToolResult:
    status:  ToolStatus
    payload: dict[str, Any] = field(default_factory=dict)
    error:   str | None = None
    # Spoken verbatim instead of letting the LLM narrate the result (anti-hallucination);
    # set by executors, e.g. from custom_apis.success_template.
    deterministic_response: str | None = None
    # Business-local datetime the deterministic success confirmed; only set with deterministic_response.
    confirmed_datetime: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    """What the LLM may know about a tool — schema only, never an executor reference."""
    name:              str
    description:       str
    parameters_schema: dict[str, Any]
    category:          str = ""
    # False = enabled per agent but never offered to the LLM.
    llm_visible:       bool = True

    def to_generic_schema(self) -> dict[str, Any]:
        """Vendor-neutral {name, description, parameters}; each LLM provider wraps it in its wire format."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters_schema}


@dataclass(frozen=True)
class ToolExecutionContext:
    """Everything an executor needs; executors are pure functions of (request, context)."""
    tenant_id:                    str
    agent_id:                     str
    call_id:                      str
    session_id:                   str
    turn_id:                      str
    tool_iteration:                int
    deadline:                     float  # time.monotonic() deadline for this call
    request_id:                   str
    # Caller ANI; empty for webcall/browser sessions.
    caller_number:                 str = ""
    conversation_history_snapshot: list[dict[str, Any]] = field(default_factory=list)
    # Per-call because executors are registered once at startup with no per-agent policy.
    max_chain_depth:                int | None = None


@dataclass(frozen=True)
class ToolExecutionRequest:
    tool_call_id:     str
    tool_name:        str
    arguments:        dict[str, Any]
    context:          ToolExecutionContext
    idempotency_key:  str = ""

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", self.tool_call_id)
