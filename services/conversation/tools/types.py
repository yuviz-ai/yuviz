"""
Core Tool Execution Framework types — see the architecture design (Tool
Execution Framework doc, 2026-07-22) for the full reasoning behind each of
these shapes. Deliberately no dependency on anything in providers/ or
pipeline.py: this module is the seam every other tools/ module and every
executor imports, and it must stay importable on its own.

ToolStatus is intentionally small (7 values) and shared by every tool,
present and future — see the doc's §07/§12 reasoning for why "unavailable"
is SUCCESS and "missing required field" reuses INVALID_ARGUMENT rather than
each new tool inventing its own status vocabulary.
"""

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
    # Set by an executor (never the orchestrator — see orchestrator.py's own
    # module docstring on staying tool-agnostic) when this exact outcome
    # must reach the caller verbatim, with zero LLM discretion over the
    # wording. When set, ToolCallOrchestrator speaks this text directly via
    # a DeterministicSpokenEvent instead of looping back to the LLM for a
    # free-text follow-up generate() call to narrate the result. Built for
    # CalendarExecutor's real booking-success case — confirmed live,
    # repeatedly, that an LLM asked to narrate "what just happened" will
    # sometimes narrate a false "booked!" instead of having actually
    # called the tool, no matter how the prompt is worded; a real success
    # must never be put in a position where it could be confused with
    # that failure mode.
    deterministic_response: str | None = None
    # The real, business-local wall-clock datetime this deterministic
    # success actually confirmed (e.g. "2026-08-31T14:00:00") — set
    # alongside deterministic_response so pipeline.py can tell a later
    # turn's TRUTHFUL recap of this exact slot apart from a NEW, unconfirmed
    # claim about a different one (e.g. a caller asking to reschedule,
    # which the LLM sometimes narrates without ever calling
    # reschedule_appointment — confirmed live). Only meaningful when
    # deterministic_response is also set.
    confirmed_datetime: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    """What the LLM is allowed to know about a tool — schema only, never an
    executor reference (see ExecutorRegistry, kept deliberately separate)."""
    name:              str
    description:       str
    parameters_schema: dict[str, Any]
    category:          str = ""
    # False for a tool an admin can configure/enable per agent but the LLM
    # must never see or call itself (e.g. send_sms — a deterministic
    # post-booking side effect, not a decision the model makes). Checked
    # by orchestrator.py when building the schemas list offered to the LLM.
    llm_visible:       bool = True
    # Another tool_name whose provider this one's executor also needs
    # (e.g. book_appointment -> send_sms) — resolved generically by
    # ToolCallOrchestrator (see its own docstring on staying provider-
    # agnostic: it reads this field rather than hardcoding any tool name).
    companion_tool_name: str | None = None

    def to_generic_schema(self) -> dict[str, Any]:
        """Vendor-neutral {name, description, parameters} shape — every
        IToolAwareLLM implementation wraps this into its own wire format
        (OpenAI/Ollama: {"type":"function","function": this}; Gemini: this
        goes straight into a functionDeclarations entry), the same way
        LLMAdapter/each provider already bridges other vendor-specific
        shape differences rather than pushing them onto callers."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters_schema}


@dataclass(frozen=True)
class ToolExecutionContext:
    """Everything an executor needs without reaching back into
    ConversationSession/pipeline state — an executor is a pure function of
    (request, context)."""
    tenant_id:                    str
    agent_id:                     str
    call_id:                      str
    session_id:                   str
    turn_id:                      str
    tool_iteration:                int
    deadline:                     float  # time.monotonic() deadline for this call
    request_id:                   str
    # The caller's real ANI (SIP caller ID), when this session has one — a
    # webcall/browser test session has none (empty string). Booking uses
    # this automatically as the attendee's phone number when present;
    # cancel/reschedule deliberately never trust it (see
    # cancel_appointment_executor.py's module docstring) — a caller phoning
    # in to cancel may not be calling from the same number they booked
    # with, so those always ask the caller to state a phone number instead.
    caller_number:                 str = ""
    # True once the caller has confirmed caller_number this call (see
    # pipeline.py's _caller_just_confirmed_phone_number) — CalendarExecutor
    # refuses to book against the ANI until this is true.
    phone_number_confirmed:        bool = False
    conversation_history_snapshot: list[dict[str, Any]] = field(default_factory=list)
    # ResolvedToolPolicy.max_chain_depth for THIS agent's execute_api policy
    # row (NULL = no override, use the platform default) — only
    # ApiExecExecutor reads this; every other executor ignores it. Carried
    # per-call, not baked into the executor at construction time, because
    # ExecutorRegistry.resolve() has no per-agent policy to close over when
    # the factory is registered once at process startup (__main__.py).
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
