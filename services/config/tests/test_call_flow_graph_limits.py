"""Issue #183 through the real route: the step cap matches the call-flow validator (200)."""

from __future__ import annotations

from services.config.tests.test_call_flows import (  # noqa: F401  (fixtures + helpers)
    HANGUP, START, _cleanup_call_flows, _client_as, _flow,
)


def _big_graph(n: int, pad: str = "Hi.") -> dict:
    nodes = [START] + [
        {"id": f"p{i}", "type": "play", "data": {"prompt": pad}} for i in range(n - 2)
    ] + [HANGUP]
    ids = [x["id"] for x in nodes]
    edges = [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(zip(ids, ids[1:]))]
    return {"version": 1, "nodes": nodes, "edges": edges}


async def test_draft_accepts_51_and_200_steps(test_tenant, test_admin):
    flow = await _flow(test_tenant)
    async with _client_as(test_admin["user"]) as client:
        for n in (51, 200):
            resp = await client.put(f"/call-flows/{flow['id']}/draft", json={"graph": _big_graph(n)})
            assert resp.status_code == 200, resp.text


async def test_draft_over_limit_is_422_naming_200_and_not_echoing_the_graph(test_tenant, test_admin):
    flow = await _flow(test_tenant)
    marker = "ECHO-MARKER-" + "z" * 40
    async with _client_as(test_admin["user"]) as client:
        resp = await client.put(
            f"/call-flows/{flow['id']}/draft", json={"graph": _big_graph(201, pad=marker)},
        )
    assert resp.status_code == 422
    assert "max 200" in resp.text
    assert marker not in resp.text
    assert len(resp.text) < 1000
