"""
Cross-check between services/toolexec/schemas.py's Pydantic Literal fields
and the DDL CHECK constraints they must never drift from (security audit
finding 10 / low #10: ChainStepReport.status omits 'claimed', which
api_chain_steps.status permits).

Parses the actual database/schema.sql CHECK constraint text rather than
hand-copying the value list, so this genuinely trips if either side changes
without the other (lesson 12's "a tripwire that cannot trip when a new case
is added" — this one can, because it reads the live DDL and the live model
instead of two independently hand-maintained lists).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from services.toolexec import graph
from services.toolexec.schemas import ChainExecuteRequest, ChainStepReport

_SCHEMA_SQL = Path(__file__).resolve().parents[3] / "database" / "schema.sql"


def _api_chain_steps_status_values() -> set[str]:
    sql = _SCHEMA_SQL.read_text()
    # Find the api_chain_steps table block, then its status CHECK within it.
    table_match = re.search(
        r"CREATE TABLE IF NOT EXISTS api_chain_steps\s*\((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert table_match is not None, "api_chain_steps table not found in schema.sql"
    table_body = table_match.group(1)
    check_match = re.search(
        r"status\s+TEXT NOT NULL CHECK \(status IN \((.*?)\)\)",
        table_body,
        re.DOTALL,
    )
    assert check_match is not None, "api_chain_steps.status CHECK not found"
    return {v.strip().strip("'") for v in check_match.group(1).split(",")}


def test_chain_step_report_status_matches_api_chain_steps_check_constraint():
    """Verified by mutation: this test passes today ONLY if every DDL value
    is also a Literal member. Confirmed it fails as expected against the
    shipped code — schemas.ChainStepReport.status's Literal is missing
    'claimed' (schemas.py:82), while the DDL CHECK (database/schema.sql,
    api_chain_steps) permits it. A chain-history read over an in-flight or
    crash-abandoned run would serialize a 'claimed' step and raise a
    ValidationError. This is a genuine source defect, reported as such —
    the fix (add 'claimed' to the Literal) is the implementer's, not made
    here."""
    ddl_values = _api_chain_steps_status_values()
    model_values = set(get_args(ChainStepReport.model_fields["status"].annotation))
    assert model_values == ddl_values


def _request_kwargs(**overrides) -> dict:
    kwargs = dict(
        tenant_id="t1", agent_id="a1", call_id="c1", session_id="s1", turn_id="turn1",
        tool_call_id="tc1", idempotency_key="idem1", api_name="x",
        chain_budget_ms=5000, max_chain_depth=4,
    )
    kwargs.update(overrides)
    return kwargs


def test_max_chain_depth_rejects_a_value_above_the_platform_ceiling():
    """FIX 4c (security finding 7): the model must not trust the caller
    already clamped max_chain_depth — a compromised or buggy allow-listed
    service account setting max_chain_depth=64 must be rejected at the
    model boundary, not merely by a downstream min()."""
    with pytest.raises(ValidationError):
        ChainExecuteRequest(**_request_kwargs(max_chain_depth=64))


def test_max_chain_depth_rejects_zero_and_negative():
    with pytest.raises(ValidationError):
        ChainExecuteRequest(**_request_kwargs(max_chain_depth=0))
    with pytest.raises(ValidationError):
        ChainExecuteRequest(**_request_kwargs(max_chain_depth=-1))


def test_max_chain_depth_at_the_platform_ceiling_is_accepted():
    request = ChainExecuteRequest(**_request_kwargs(max_chain_depth=graph.MAX_CHAIN_LEVELS))
    assert request.max_chain_depth == graph.MAX_CHAIN_LEVELS


def test_resolve_order_clamps_max_levels_to_the_platform_ceiling_even_if_a_caller_forgets():
    """FIX 4c: the ceiling lives in the pure function itself, not just in
    a caller's comment — a 5-level chain must still be rejected even when
    called with max_levels far above graph.MAX_CHAIN_LEVELS."""
    leaf = {"id": "1", "name": "l1", "upstream_apis": []}
    node = leaf
    for i in range(2, 6):
        node = {"id": str(i), "name": f"l{i}", "upstream_apis": [node]}  # now 5 levels deep

    with pytest.raises(ValueError, match="depth_limit_exceeded"):
        graph.resolve_order(node, max_levels=64)
