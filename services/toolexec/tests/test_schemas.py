"""Cross-check schemas.py Literal fields against the live schema.sql CHECK constraints."""

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
    """ChainStepReport.status Literal equals the api_chain_steps.status CHECK values."""
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
    """max_chain_depth above the ceiling is rejected at the model boundary."""
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
    """resolve_order rejects a 5-level chain even when passed max_levels=64."""
    leaf = {"id": "1", "name": "l1", "upstream_apis": []}
    node = leaf
    for i in range(2, 6):
        node = {"id": str(i), "name": f"l{i}", "upstream_apis": [node]}  # now 5 levels deep

    with pytest.raises(ValueError, match="depth_limit_exceeded"):
        graph.resolve_order(node, max_levels=64)
