"""Graph size bounds on call-flow vs workflow request models; 422 bodies must not echo input."""

from __future__ import annotations

import json

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from libs.config_sdk.callflow import MAX_NODES
from services.config import schemas
from services.config.app import _validation_error_handler


def _graph(n_nodes: int, pad: str = "") -> dict:
    return {
        "version": 1,
        "nodes": [{"id": f"n{i}", "type": "play", "data": {"text": pad}} for i in range(n_nodes)],
        "edges": [],
    }


BUILDERS = [
    lambda g: schemas.CallFlowDraft(graph=g),
    lambda g: schemas.CallFlowPublish(graph=g),
]


def test_call_flow_limit_is_the_validator_limit():
    assert schemas.MAX_CALL_FLOW_NODES == MAX_NODES == 200


@pytest.mark.parametrize("build", BUILDERS)
def test_call_flow_accepts_51_and_exactly_max_nodes(build):
    build(_graph(51))
    build(_graph(MAX_NODES))


@pytest.mark.parametrize("build", BUILDERS)
def test_call_flow_rejects_over_max_nodes_naming_the_real_limit(build):
    with pytest.raises(ValidationError) as ei:
        build(_graph(MAX_NODES + 1))
    assert f"max {MAX_NODES}" in str(ei.value)


def test_agent_workflow_limit_unchanged():
    schemas.WorkflowDraft(graph=_graph(schemas.MAX_WORKFLOW_NODES))
    with pytest.raises(ValidationError) as ei:
        schemas.WorkflowDraft(graph=_graph(schemas.MAX_WORKFLOW_NODES + 1))
    assert f"max {schemas.MAX_WORKFLOW_NODES}" in str(ei.value)


def test_non_list_nodes_is_not_a_bounds_error():
    # parse_graph reports malformed shapes; the bounds check must not crash on them.
    schemas.CallFlowDraft(graph={"nodes": "x", "edges": None})


@pytest.mark.asyncio
async def test_422_body_does_not_echo_submitted_graph():
    marker = "SECRET-MARKER-" + "x" * 50
    with pytest.raises(ValidationError) as ei:
        schemas.CallFlowDraft(graph=_graph(MAX_NODES + 1, pad=marker))
    resp = await _validation_error_handler(None, RequestValidationError(ei.value.errors()))
    body = resp.body.decode()
    assert resp.status_code == 422
    assert marker not in body
    assert len(body) < 1000
    detail = json.loads(body)["detail"]
    assert detail and detail[0]["loc"] and "max 200" in detail[0]["msg"]
    assert set(detail[0]) <= {"type", "loc", "msg"}


@pytest.mark.asyncio
async def test_422_for_non_graph_errors_keeps_shape_without_input():
    with pytest.raises(ValidationError) as ei:
        schemas.TenantUpdate(knowledge_max_documents=0)
    resp = await _validation_error_handler(None, RequestValidationError(ei.value.errors()))
    detail = json.loads(resp.body)["detail"]
    assert resp.status_code == 422
    assert detail[0]["loc"] and detail[0]["msg"] and "input" not in detail[0]
