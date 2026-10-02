"""TransferDecisionEngine — decides *whether* a trigger becomes a human transfer and builds the TransferRequest.
Pure and stateless: ConversationFSM owns session state, the pipeline/servicer own dispatch."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .directives import TransferDirective, TransferRequest, TransferType, coerce_transfer_type
from .metrics import IMetrics, NullMetrics

log = logging.getLogger(__name__)


class TransferReason(str, Enum):
    """Internal reason vocabulary; .wire_trigger is the stable string sent as TransferRequest.trigger."""
    CUSTOMER_REQUEST     = "CUSTOMER_REQUEST"
    ESCALATION_THRESHOLD = "ESCALATION_THRESHOLD"
    WORKFLOW_POLICY      = "WORKFLOW_POLICY"
    SYSTEM_REQUEST       = "SYSTEM_REQUEST"

    @property
    def wire_trigger(self) -> str:
        return _WIRE_TRIGGER[self]


_WIRE_TRIGGER: dict["TransferReason", str] = {
    TransferReason.CUSTOMER_REQUEST:     "llm_directive",
    TransferReason.ESCALATION_THRESHOLD: "escalation_threshold",
    TransferReason.WORKFLOW_POLICY:      "workflow_policy",
    TransferReason.SYSTEM_REQUEST:       "system_request",
}


class TriggerType(str, Enum):
    LLM_DIRECTIVE = "llm_directive"
    ESCALATION    = "escalation"
    WORKFLOW      = "workflow"
    EXTERNAL      = "external"


@dataclass(frozen=True)
class TransferTrigger:
    """A possible-transfer event; only the payload field matching `type` is meaningful."""
    type: TriggerType
    directive: TransferDirective | None = None
    violation_count: int | None = None
    workflow_reason: str | None = None
    external_reason: str | None = None


@dataclass(frozen=True)
class DecisionContext:
    """Everything evaluate() needs, supplied by the caller.
    already_requested blocks a second trigger firing before the first transfer resolves."""
    session_id: str
    tenant_id: str
    call_id: str
    transfer_type: str                # RuntimeConfig.policies.transfer_type: "none"/"cold"/"warm"
    transfer_destination: str | None
    escalation_threshold: int | None
    already_requested: bool = False
    # Caller-ID inputs; warm transfer only (cold never originates a new leg).
    caller_id_policy: str = "original"
    platform_did: str | None = None
    custom_caller_id: str | None = None
    caller_number: str = ""
    waiting_experience: str = "announcement_moh"


@dataclass(frozen=True)
class Decision:
    accepted: bool
    request: TransferRequest | None = None
    rejection_reason: str | None = None


def _resolve_caller_id(ctx: DecisionContext) -> str:
    """Caller ID shown to the human agent; unknown/misconfigured policies fall back to the caller's ANI."""
    if ctx.caller_id_policy == "platform" and ctx.platform_did:
        return ctx.platform_did
    if ctx.caller_id_policy == "custom" and ctx.custom_caller_id:
        return ctx.custom_caller_id
    return ctx.caller_number


class TransferDecisionEngine:
    """Stateless; instances only carry the metrics sink."""

    def __init__(self, metrics: IMetrics | None = None) -> None:
        self._metrics = metrics if metrics is not None else NullMetrics()

    def evaluate(self, ctx: DecisionContext, trigger: TransferTrigger) -> Decision:
        self._metrics.increment(f"transfer_decision_total.{ctx.tenant_id}")

        def reject(rejection_reason: str) -> Decision:
            log.info(
                "Transfer decision rejected trigger=%s reason=%s tenant_id=%s "
                "session_id=%s call_id=%s",
                trigger.type.value, rejection_reason, ctx.tenant_id,
                ctx.session_id, ctx.call_id,
            )
            self._metrics.increment(f"transfer_decision_rejected_total.{ctx.tenant_id}.{rejection_reason}")
            if rejection_reason == "already_transferring":
                self._metrics.increment(f"transfer_duplicate_total.{ctx.tenant_id}")
            return Decision(accepted=False, rejection_reason=rejection_reason)

        if ctx.already_requested:
            return reject("already_transferring")

        if trigger.type is TriggerType.LLM_DIRECTIVE:
            if trigger.directive is None:
                return reject("missing_directive_payload")
            transfer_reason = TransferReason.CUSTOMER_REQUEST
            transfer_type   = trigger.directive.transfer_type
            destination     = trigger.directive.destination
            free_reason     = trigger.directive.reason

        elif trigger.type is TriggerType.ESCALATION:
            if trigger.violation_count is None:
                return reject("missing_violation_count")
            self._metrics.observe(
                f"escalation_counter_current.{ctx.tenant_id}", float(trigger.violation_count),
            )
            if ctx.escalation_threshold is None or trigger.violation_count <= ctx.escalation_threshold:
                return reject("threshold_not_reached")
            transfer_reason = TransferReason.ESCALATION_THRESHOLD
            transfer_type   = coerce_transfer_type(ctx.transfer_type)
            destination     = ctx.transfer_destination or ""
            free_reason     = f"escalation_threshold_exceeded (violations={trigger.violation_count})"

        elif trigger.type is TriggerType.WORKFLOW:
            transfer_reason = TransferReason.WORKFLOW_POLICY
            transfer_type   = coerce_transfer_type(ctx.transfer_type)
            destination     = ctx.transfer_destination or ""
            free_reason     = trigger.workflow_reason or "workflow_policy"

        elif trigger.type is TriggerType.EXTERNAL:
            transfer_reason = TransferReason.SYSTEM_REQUEST
            transfer_type   = coerce_transfer_type(ctx.transfer_type)
            destination     = ctx.transfer_destination or ""
            free_reason     = trigger.external_reason or "system_request"

        else:  # pragma: no cover — exhaustive over TriggerType
            return reject(f"unknown_trigger_type:{trigger.type!r}")

        if transfer_type == TransferType.NONE:
            return reject("transfer_disabled")
        if not destination:
            return reject("no_destination_configured")

        # caller_id is ignored by the gateway for cold transfers.
        request = TransferRequest(
            session_id=ctx.session_id, tenant_id=ctx.tenant_id, call_id=ctx.call_id,
            transfer_type=transfer_type, destination=destination, reason=free_reason,
            trigger=transfer_reason.wire_trigger, caller_id=_resolve_caller_id(ctx),
            waiting_experience=ctx.waiting_experience,
        )
        log.info(
            "Transfer decision accepted trigger=%s type=%s destination=%s "
            "transfer_id=%s tenant_id=%s session_id=%s call_id=%s",
            transfer_reason.wire_trigger, transfer_type.value, destination,
            request.transfer_id, ctx.tenant_id, ctx.session_id, ctx.call_id,
        )
        self._metrics.increment(f"transfer_decision_accepted_total.{ctx.tenant_id}")
        return Decision(accepted=True, request=request)
