"""Review finding on #183: call flows must not share the agent-workflow 120-edge cap."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from libs.config_sdk.callflow import MAX_NODES
from services.config import schemas
from services.config.tests.test_call_flows import (  # noqa: F401  (fixtures + helpers)
    _cleanup_call_flows, _client_as, _flow,
)

BUILDERS = [
    lambda g: schemas.CallFlowDraft(graph=g),
    lambda g: schemas.CallFlowPublish(graph=g),
]


def _graph(n: int, n_edges: int | None = None) -> dict:
    nodes = [{"id": f"n{i}", "type": "play", "data": {}} for i in range(n)]
    edges = [{"id": f"e{i}", "source": f"n{i}", "target": f"n{i + 1}"} for i in range(n - 1)]
    if n_edges is not None:
        edges = [{"id": f"e{i}", "source": "n0", "target": "n1"} for i in range(n_edges)]
    return {"version": 1, "nodes": nodes, "edges": edges}


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("n", [150, MAX_NODES])
def test_connected_chain_is_accepted(build, n):
    build(_graph(n))


@pytest.mark.parametrize("build", BUILDERS)
def test_over_edge_cap_is_rejected_naming_the_cap(build):
    with pytest.raises(ValidationError) as ei:
        build(_graph(10, n_edges=schemas.MAX_CALL_FLOW_EDGES + 1))
    assert f"max {schemas.MAX_CALL_FLOW_EDGES}" in str(ei.value)


def test_agent_workflow_edge_cap_unchanged():
    assert schemas.MAX_WORKFLOW_EDGES == 120
    with pytest.raises(ValidationError):
        schemas.WorkflowDraft(graph=_graph(10, n_edges=121))


async def test_route_accepts_connected_150_and_200_step_chain(test_tenant, test_admin):
    flow = await _flow(test_tenant)
    async with _client_as(test_admin["user"]) as client:
        for n in (150, MAX_NODES):
            g = _graph(n)
            assert len(g["edges"]) == n - 1
            resp = await client.put(f"/call-flows/{flow['id']}/draft", json={"graph": g})
            assert resp.status_code == 200, resp.text
