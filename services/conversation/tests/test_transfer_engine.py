"""TransferDecisionEngine: the single, stateless arbiter of when to transfer."""

from __future__ import annotations

from ..directives import TransferDirective, TransferType
from ..transfer_engine import (
    DecisionContext,
    TransferDecisionEngine,
    TransferReason,
    TransferTrigger,
    TriggerType,
)


def _ctx(**overrides) -> DecisionContext:
    defaults = dict(
        session_id="s1", tenant_id="acme", call_id="c1",
        transfer_type="cold", transfer_destination="1001",
        escalation_threshold=1, already_requested=False,
    )
    return DecisionContext(**{**defaults, **overrides})


class _RecordingMetrics:
    def __init__(self) -> None:
        self.increments: list[tuple[str, float]] = []
        self.observations: list[tuple[str, float]] = []

    def increment(self, name: str, value: float = 1.0) -> None:
        self.increments.append((name, value))

    def observe(self, name: str, value: float) -> None:
        self.observations.append((name, value))

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.increments if n == name)


# ---------------------------------------------------------------------------
# LLM directive trigger
# ---------------------------------------------------------------------------

def test_llm_directive_accepted_when_configured():
    engine = TransferDecisionEngine()
    directive = TransferDirective(
        transfer_type=TransferType.COLD, destination="1001", reason="caller_requested_human",
    )
    decision = engine.evaluate(_ctx(), TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=directive))
    assert decision.accepted
    assert decision.request is not None
    assert decision.request.transfer_type == TransferType.COLD
    assert decision.request.destination == "1001"
    assert decision.request.trigger == "llm_directive"  # unchanged wire value


def test_llm_directive_uses_directives_own_type_and_destination_not_context():
    """An llm_directive's own type/destination override the agent's configured default."""
    engine = TransferDecisionEngine()
    directive = TransferDirective(
        transfer_type=TransferType.COLD, destination="+15559990000", reason="explicit",
    )
    decision = engine.evaluate(
        _ctx(transfer_type="cold", transfer_destination="1001"),
        TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=directive),
    )
    assert decision.accepted
    assert decision.request.destination == "+15559990000"


def test_llm_directive_missing_payload_rejected():
    engine = TransferDecisionEngine()
    decision = engine.evaluate(_ctx(), TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=None))
    assert not decision.accepted
    assert decision.rejection_reason == "missing_directive_payload"


def test_llm_directive_rejected_when_transfer_type_none_in_directive():
    engine = TransferDecisionEngine()
    directive = TransferDirective(transfer_type=TransferType.NONE, destination="1001", reason="x")
    decision = engine.evaluate(_ctx(), TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=directive))
    assert not decision.accepted
    assert decision.rejection_reason == "transfer_disabled"


# ---------------------------------------------------------------------------
# Escalation trigger
# ---------------------------------------------------------------------------

def test_escalation_accepted_once_threshold_exceeded():
    engine = TransferDecisionEngine()
    ctx = _ctx(escalation_threshold=1)
    decision = engine.evaluate(ctx, TransferTrigger(type=TriggerType.ESCALATION, violation_count=2))
    assert decision.accepted
    assert decision.request.trigger == "escalation_threshold"
    assert "violations=2" in decision.request.reason
    assert decision.request.destination == "1001"


def test_escalation_not_yet_at_threshold_rejected():
    engine = TransferDecisionEngine()
    ctx = _ctx(escalation_threshold=2)
    decision = engine.evaluate(ctx, TransferTrigger(type=TriggerType.ESCALATION, violation_count=2))
    assert not decision.accepted
    assert decision.rejection_reason == "threshold_not_reached"


def test_escalation_disabled_when_threshold_none():
    engine = TransferDecisionEngine()
    ctx = _ctx(escalation_threshold=None)
    decision = engine.evaluate(ctx, TransferTrigger(type=TriggerType.ESCALATION, violation_count=100))
    assert not decision.accepted
    assert decision.rejection_reason == "threshold_not_reached"


def test_escalation_missing_violation_count_rejected():
    engine = TransferDecisionEngine()
    decision = engine.evaluate(_ctx(), TransferTrigger(type=TriggerType.ESCALATION, violation_count=None))
    assert not decision.accepted
    assert decision.rejection_reason == "missing_violation_count"


# ---------------------------------------------------------------------------
# Workflow / external triggers (no producer exists yet — engine-level only)
# ---------------------------------------------------------------------------

def test_workflow_trigger_accepted_when_transfer_configured():
    engine = TransferDecisionEngine()
    decision = engine.evaluate(
        _ctx(), TransferTrigger(type=TriggerType.WORKFLOW, workflow_reason="account_locked"),
    )
    assert decision.accepted
    assert decision.request.trigger == "workflow_policy"
    assert decision.request.reason == "account_locked"


def test_external_trigger_accepted_when_transfer_configured():
    engine = TransferDecisionEngine()
    decision = engine.evaluate(
        _ctx(), TransferTrigger(type=TriggerType.EXTERNAL, external_reason="supervisor_override"),
    )
    assert decision.accepted
    assert decision.request.trigger == "system_request"
    assert decision.request.reason == "supervisor_override"


# ---------------------------------------------------------------------------
# Universal eligibility checks (apply to every trigger type)
# ---------------------------------------------------------------------------

def test_disabled_transfer_type_rejected_for_escalation():
    engine = TransferDecisionEngine()
    ctx = _ctx(transfer_type="none", escalation_threshold=1)
    decision = engine.evaluate(ctx, TransferTrigger(type=TriggerType.ESCALATION, violation_count=5))
    assert not decision.accepted
    assert decision.rejection_reason == "transfer_disabled"


def test_missing_destination_rejected_for_escalation():
    engine = TransferDecisionEngine()
    ctx = _ctx(transfer_destination=None, escalation_threshold=1)
    decision = engine.evaluate(ctx, TransferTrigger(type=TriggerType.ESCALATION, violation_count=5))
    assert not decision.accepted
    assert decision.rejection_reason == "no_destination_configured"


def test_missing_destination_rejected_for_workflow():
    engine = TransferDecisionEngine()
    ctx = _ctx(transfer_destination=None)
    decision = engine.evaluate(ctx, TransferTrigger(type=TriggerType.WORKFLOW, workflow_reason="x"))
    assert not decision.accepted
    assert decision.rejection_reason == "no_destination_configured"


def test_already_requested_rejects_any_trigger_type():
    engine = TransferDecisionEngine()
    ctx = _ctx(already_requested=True)
    directive = TransferDirective(transfer_type=TransferType.COLD, destination="1001", reason="x")
    decision = engine.evaluate(ctx, TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=directive))
    assert not decision.accepted
    assert decision.rejection_reason == "already_transferring"


# ---------------------------------------------------------------------------
# Statelessness — the engine's own contract
# ---------------------------------------------------------------------------

def test_engine_is_pure_same_inputs_same_output():
    engine = TransferDecisionEngine()
    ctx = _ctx()
    trigger = TransferTrigger(type=TriggerType.ESCALATION, violation_count=5)
    first = engine.evaluate(ctx, trigger)
    second = engine.evaluate(ctx, trigger)
    assert first.accepted == second.accepted
    assert first.rejection_reason == second.rejection_reason
    # New transfer_id per call, otherwise identical: the engine keeps no memory.
    assert first.request.transfer_id != second.request.transfer_id
    assert first.request.destination == second.request.destination


def test_engine_holds_no_session_state_between_calls():
    """A prior accepted decision for one session doesn't affect another's."""
    engine = TransferDecisionEngine()
    engine.evaluate(_ctx(session_id="s1"), TransferTrigger(type=TriggerType.ESCALATION, violation_count=5))
    decision = engine.evaluate(
        _ctx(session_id="s2", already_requested=False),
        TransferTrigger(type=TriggerType.ESCALATION, violation_count=5),
    )
    assert decision.accepted


# ---------------------------------------------------------------------------
# TransferReason wire-string mapping (no protocol change)
# ---------------------------------------------------------------------------

def test_reason_wire_values_preserve_existing_strings():
    assert TransferReason.CUSTOMER_REQUEST.wire_trigger == "llm_directive"
    assert TransferReason.ESCALATION_THRESHOLD.wire_trigger == "escalation_threshold"


def test_reason_wire_values_for_new_triggers():
    assert TransferReason.WORKFLOW_POLICY.wire_trigger == "workflow_policy"
    assert TransferReason.SYSTEM_REQUEST.wire_trigger == "system_request"


# ---------------------------------------------------------------------------
# Metrics — unlabeled-but-tenant-prefixed counters via existing IMetrics
# ---------------------------------------------------------------------------

def test_metrics_emitted_on_acceptance():
    metrics = _RecordingMetrics()
    engine = TransferDecisionEngine(metrics)
    directive = TransferDirective(transfer_type=TransferType.COLD, destination="1001", reason="x")
    engine.evaluate(_ctx(), TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=directive))
    assert metrics.count("transfer_decision_total.acme") == 1
    assert metrics.count("transfer_decision_accepted_total.acme") == 1
    assert metrics.count("transfer_decision_rejected_total.acme") == 0


def test_metrics_emitted_on_rejection():
    metrics = _RecordingMetrics()
    engine = TransferDecisionEngine(metrics)
    engine.evaluate(_ctx(transfer_type="none"), TransferTrigger(type=TriggerType.WORKFLOW, workflow_reason="x"))
    assert metrics.count("transfer_decision_total.acme") == 1
    assert metrics.count("transfer_decision_rejected_total.acme.transfer_disabled") == 1


def test_duplicate_metric_emitted_only_for_already_requested_rejection():
    metrics = _RecordingMetrics()
    engine = TransferDecisionEngine(metrics)
    directive = TransferDirective(transfer_type=TransferType.COLD, destination="1001", reason="x")
    engine.evaluate(
        _ctx(already_requested=True), TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=directive),
    )
    assert metrics.count("transfer_duplicate_total.acme") == 1

    metrics2 = _RecordingMetrics()
    engine2 = TransferDecisionEngine(metrics2)
    engine2.evaluate(_ctx(transfer_type="none"), TransferTrigger(type=TriggerType.WORKFLOW, workflow_reason="x"))
    assert metrics2.count("transfer_duplicate_total.acme") == 0


def test_escalation_counter_gauge_observed():
    metrics = _RecordingMetrics()
    engine = TransferDecisionEngine(metrics)
    engine.evaluate(_ctx(escalation_threshold=5), TransferTrigger(type=TriggerType.ESCALATION, violation_count=3))
    assert ("escalation_counter_current.acme", 3.0) in metrics.observations


def test_no_metrics_sink_defaults_to_null_metrics_no_crash():
    engine = TransferDecisionEngine()  # no metrics arg
    directive = TransferDirective(transfer_type=TransferType.COLD, destination="1001", reason="x")
    decision = engine.evaluate(_ctx(), TransferTrigger(type=TriggerType.LLM_DIRECTIVE, directive=directive))
    assert decision.accepted
