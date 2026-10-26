"""Call-flow (IVR) service and the runtime /published route, against real Postgres + Redis."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.tenancy import current_scope, set_caller_tenant, set_target_tenant
from services.config import agents, auth, cache, call_flows, provider_configs
from services.config.app import app


@contextmanager
def _as_tenant(tenant_id):
    """Sets the RLS scope a real request would, for direct service-layer calls."""
    prev = current_scope()
    set_caller_tenant(None)
    set_target_tenant(str(tenant_id))
    try:
        yield
    finally:
        set_caller_tenant(prev.caller)
        set_target_tenant(prev.target)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_call_flows(pool, test_tenant):
    """call_flows.tenant_id is ON DELETE RESTRICT, so flows must go before test_tenant."""
    yield
    await pool.execute(
        "DELETE FROM call_flow_versions WHERE call_flow_id IN (SELECT id FROM call_flows WHERE tenant_id = $1)",
        test_tenant["id"],
    )
    await pool.execute("DELETE FROM call_flows WHERE tenant_id = $1", test_tenant["id"])

START = {"id": "n1", "type": "start", "data": {}}
HANGUP = {"id": "n2", "type": "hangup", "data": {"prompt": "Bye."}}
GRAPH = {"version": 1, "nodes": [START, HANGUP], "edges": [{"id": "e1", "source": "n1", "target": "n2"}]}


def _client_as(user: dict) -> AsyncClient:
    token = auth.create_access_token(user)
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest_asyncio.fixture
async def service_account(pool):
    """Platform-scoped viewer service account; a real row, since get_current_user() re-reads it."""
    user_id = str(uuid.uuid4())
    email = f"test-svc-{uuid.uuid4().hex[:8]}@example.com"
    await pool.execute(
        "INSERT INTO users (id, email, password_hash, role, tenant_id, is_service_account) "
        "VALUES ($1, $2, 'x', 'viewer', NULL, true)",
        user_id, email,
    )
    yield {"id": user_id, "email": email, "role": "viewer", "tenant_id": None, "is_service_account": True}
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user_id)


async def _create_tenant(pool, *, name: str = "Other Tenant") -> dict:
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        name, f"test-{uuid.uuid4().hex[:8]}",
    )
    return dict(row)


async def _flow(test_tenant, *, graph=GRAPH, direction="inbound"):
    with _as_tenant(test_tenant["id"]):
        return await call_flows.create_call_flow(
            tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}",
            name="Test Flow", direction=direction, graph=graph,
        )


async def test_publish_invalidates_cache_and_bumps_config_version(test_tenant):
    flow = await _flow(test_tenant)
    with _as_tenant(test_tenant["id"]):
        first = await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"])
        assert first["config_version"] == flow["config_version"]

        published = await call_flows.publish(flow["id"], GRAPH)
        assert published["config_version"] == first["config_version"] + 1

        second = await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"])
        assert second["config_version"] == published["config_version"]


async def test_delete_invalidates_cache(test_tenant):
    flow = await _flow(test_tenant)
    with _as_tenant(test_tenant["id"]):
        assert await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"]) is not None

        await call_flows.delete_call_flow(flow["id"])

        key = call_flows._runtime_cache_key(test_tenant["slug"], flow["id"])
        assert await cache.get_json(key) is None
        assert await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"]) is None


async def test_payload_resolves_same_tenant_active_agent_and_omits_cross_tenant_or_inactive(test_tenant, pool):
    other_tenant = await _create_tenant(pool)
    with _as_tenant(test_tenant["id"]):
        same_tenant_agent = await agents.create_agent(
            tenant_id=test_tenant["id"], slug="picked-up", name="Same Tenant Agent",
        )
        inactive_agent = await agents.create_agent(
            tenant_id=test_tenant["id"], slug="inactive-one", name="Inactive Agent",
        )
    with _as_tenant(test_tenant["id"]):
        moved_agent = await agents.create_agent(
            tenant_id=test_tenant["id"], slug="planted", name="Planted",
        )
    other_tenant_agent_id = str(moved_agent["id"])

    # "agent" is a terminal node type, so each candidate needs its own
    # out-edge from a menu branch rather than chaining agent -> agent.
    graph = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "start", "data": {}},
            {"id": "m1", "type": "menu", "data": {"prompt": "Pick one."}},
            {"id": "a1", "type": "agent", "data": {"agent_id": str(same_tenant_agent["id"])}},
            {"id": "a2", "type": "agent", "data": {"agent_id": str(inactive_agent["id"])}},
            {"id": "a3", "type": "agent", "data": {"agent_id": str(other_tenant_agent_id)}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "m1"},
            {"id": "e2", "source": "m1", "target": "a1", "data": {"key": "1"}},
            {"id": "e3", "source": "m1", "target": "a2", "data": {"key": "2"}},
            {"id": "e4", "source": "m1", "target": "a3", "data": {"key": "3"}},
        ],
    }
    # Publish while all three resolve, then take two away: publish() refuses unavailable agents.
    with _as_tenant(test_tenant["id"]):
        flow = await call_flows.create_call_flow(
            tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}", name="Agent Flow", graph=graph,
        )
    assert flow["graph"] is not None, "all three agents resolved, so this must publish"

    await pool.execute("UPDATE agents SET status = 'inactive' WHERE id = $1", inactive_agent["id"])
    # Moved so the id names a real row in the wrong tenant; an unscoped query would leak it.
    await pool.execute("UPDATE agents SET tenant_id = $2 WHERE id = $1",
                       other_tenant_agent_id, other_tenant["id"])

    with _as_tenant(test_tenant["id"]):
        payload = await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"])
    assert payload["agent_slugs"] == {str(same_tenant_agent["id"]): "picked-up"}

    await pool.execute("DELETE FROM agents WHERE id = $1", other_tenant_agent_id)
    await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


async def test_resolved_tts_config_id_null_for_cross_tenant_or_non_tts_provider(test_tenant, pool):
    other_tenant = await _create_tenant(pool)
    with _as_tenant(test_tenant["id"]):
        same_tenant_tts = await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="TTS", role="tts", engine="elevenlabs",
            allow_pointer_schemes=False,
        )
        same_tenant_stt = await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="STT", role="stt", engine="deepgram",
            allow_pointer_schemes=False,
        )
    with _as_tenant(other_tenant["id"]):
        other_tenant_tts = await provider_configs.create_provider_config(
            tenant_id=other_tenant["id"], name="Other TTS", role="tts", engine="elevenlabs",
            allow_pointer_schemes=False,
        )

    async def _flow_with_start_voice(tts_config_id):
        graph = {
            "version": 1,
            "nodes": [{"id": "n1", "type": "start", "data": {"tts_config_id": tts_config_id}}, HANGUP],
            "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
        }
        with _as_tenant(test_tenant["id"]):
            return await call_flows.create_call_flow(
                tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}", name="Voice Flow", graph=graph,
            )

    same_tenant_flow = await _flow_with_start_voice(str(same_tenant_tts["id"]))
    cross_tenant_flow = await _flow_with_start_voice(str(other_tenant_tts["id"]))
    non_tts_flow = await _flow_with_start_voice(str(same_tenant_stt["id"]))

    with _as_tenant(test_tenant["id"]):
        same = await call_flows.get_published_for_runtime(test_tenant["slug"], same_tenant_flow["id"])
        cross = await call_flows.get_published_for_runtime(test_tenant["slug"], cross_tenant_flow["id"])
        non_tts = await call_flows.get_published_for_runtime(test_tenant["slug"], non_tts_flow["id"])

    assert same["resolved_tts_config_id"] == str(same_tenant_tts["id"])
    assert cross["resolved_tts_config_id"] is None
    assert non_tts["resolved_tts_config_id"] is None

    await pool.execute("DELETE FROM provider_configs WHERE id = $1", other_tenant_tts["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


async def test_published_route_returns_payload_for_service_account(test_tenant, service_account):
    flow = await _flow(test_tenant)
    async with _client_as(service_account) as client:
        resp = await client.get(f"/tenants/{test_tenant['slug']}/call-flows/{flow['id']}/published")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(flow["id"])


async def test_published_route_404_is_invariant_for_tenant_admin_regardless_of_target(
    test_tenant, test_admin, pool,
):
    """For a fixed foreign slug, a tenant admin gets an identical 404 whatever the flow's state."""
    other_tenant = await _create_tenant(pool)  # tenant "A": real, but not test_admin's
    real_flow = await _flow(other_tenant)
    with _as_tenant(other_tenant["id"]):
        draft_flow = await call_flows.create_call_flow(
            tenant_id=other_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}", name="Draft",
            graph={"version": 1, "nodes": [{"id": "a1", "type": "agent", "data": {}}], "edges": []},
        )
    outbound_flow = await _flow(other_tenant, direction="outbound")
    deleted_flow = await _flow(other_tenant)
    with _as_tenant(other_tenant["id"]):
        await call_flows.delete_call_flow(deleted_flow["id"])

    slug = other_tenant["slug"]  # held fixed across every sub-case below
    async with _client_as(test_admin["user"]) as client:
        responses = [
            await client.get(f"/tenants/{slug}/call-flows/{real_flow['id']}/published"),
            await client.get(f"/tenants/{slug}/call-flows/{uuid.uuid4()}/published"),
            await client.get(f"/tenants/{slug}/call-flows/{draft_flow['id']}/published"),
            await client.get(f"/tenants/{slug}/call-flows/{outbound_flow['id']}/published"),
            await client.get(f"/tenants/{slug}/call-flows/{deleted_flow['id']}/published"),
        ]

    for resp in responses:
        assert resp.status_code == 404
    first_body = responses[0].json()
    assert all(r.json() == first_body for r in responses[1:])  # byte-identical: same slug, any flow state

    await pool.execute("DELETE FROM call_flows WHERE tenant_id = $1", other_tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _detail_with_id_normalised(resp) -> str:
    """404 detail with the echoed (caller-supplied) id replaced, to compare message shape only."""
    body = resp.json()
    detail = body["detail"] if isinstance(body, dict) and "detail" in body else str(body)
    return _UUID_RE.sub("<id>", detail)


async def test_published_route_404_is_invariant_for_service_account_regardless_of_target(
    test_tenant, pool, service_account,
):
    """For the service account, unpublished/outbound/deleted/unknown flows all get the same 404 shape."""
    with _as_tenant(test_tenant["id"]):
        flow = await call_flows.create_call_flow(
            tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}", name="Mutable", graph=GRAPH,
        )
    flow_id = flow["id"]
    key = call_flows._runtime_cache_key(test_tenant["slug"], flow_id)
    url = f"/tenants/{test_tenant['slug']}/call-flows/{flow_id}/published"

    async with _client_as(service_account) as client:
        await pool.execute("UPDATE call_flows SET graph = NULL WHERE id = $1", flow_id)
        await cache.invalidate(key)
        unpublished_resp = await client.get(url)

        await pool.execute(
            "UPDATE call_flows SET graph = $2::jsonb, direction = 'outbound' WHERE id = $1",
            flow_id, json.dumps(GRAPH),
        )
        await cache.invalidate(key)
        outbound_resp = await client.get(url)

        await pool.execute(
            "UPDATE call_flows SET direction = 'inbound', deleted_at = now() WHERE id = $1", flow_id,
        )
        await cache.invalidate(key)
        deleted_resp = await client.get(url)

        unknown_id_resp = await client.get(
            f"/tenants/{test_tenant['slug']}/call-flows/{uuid.uuid4()}/published"
        )

    for resp in (unpublished_resp, outbound_resp, deleted_resp, unknown_id_resp):
        assert resp.status_code == 404
    # Same call_flow_id across the first three -> byte-identical bodies.
    assert unpublished_resp.json() == outbound_resp.json() == deleted_resp.json()
    # Different (unknown) id in the fourth -> compare with the id normalised out.
    normalised = {_detail_with_id_normalised(r) for r in
                  (unpublished_resp, outbound_resp, deleted_resp, unknown_id_resp)}
    assert len(normalised) == 1, normalised

    await pool.execute("DELETE FROM call_flows WHERE id = $1", flow_id)


async def test_published_route_never_403s_for_either_caller_shape(test_tenant, test_admin, pool, service_account):
    """Both caller shapes get 404, never 403 (which would confirm existence), for a foreign flow."""
    other_tenant = await _create_tenant(pool)
    other_flow = await _flow(other_tenant)

    async with _client_as(test_admin["user"]) as client:
        admin_resp = await client.get(
            f"/tenants/{other_tenant['slug']}/call-flows/{other_flow['id']}/published"
        )
    async with _client_as(service_account) as client:
        service_resp = await client.get(
            f"/tenants/{test_tenant['slug']}/call-flows/{other_flow['id']}/published"
        )

    assert admin_resp.status_code == 404
    assert service_resp.status_code == 404

    await pool.execute("DELETE FROM call_flows WHERE id = $1", other_flow["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


async def test_published_route_404s_for_unpublished_and_outbound_and_soft_deleted(test_tenant, pool, service_account):
    with _as_tenant(test_tenant["id"]):
        draft_flow = await call_flows.create_call_flow(
            tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}", name="Draft",
            graph={"version": 1, "nodes": [{"id": "a1", "type": "agent", "data": {}}], "edges": []},
        )
    assert draft_flow["graph"] is None  # invalid scaffold landed as a draft, never published

    outbound_flow = await _flow(test_tenant, direction="outbound")
    deleted_flow = await _flow(test_tenant)
    with _as_tenant(test_tenant["id"]):
        await call_flows.delete_call_flow(deleted_flow["id"])

    async with _client_as(service_account) as client:
        draft_resp = await client.get(f"/tenants/{test_tenant['slug']}/call-flows/{draft_flow['id']}/published")
        outbound_resp = await client.get(f"/tenants/{test_tenant['slug']}/call-flows/{outbound_flow['id']}/published")
        deleted_resp = await client.get(f"/tenants/{test_tenant['slug']}/call-flows/{deleted_flow['id']}/published")

    assert draft_resp.status_code == 404
    assert outbound_resp.status_code == 404
    assert deleted_resp.status_code == 404


async def test_publish_refuses_an_agent_node_naming_an_unavailable_agent(test_tenant, pool):
    """agent_id lives in graph JSONB (no FK), so publish must reject unavailable or foreign agents."""
    other_tenant = await _create_tenant(pool)
    with _as_tenant(test_tenant["id"]):
        live = await agents.create_agent(
            tenant_id=test_tenant["id"], slug="still-here", name="Live Agent",
        )
        going_away = await agents.create_agent(
            tenant_id=test_tenant["id"], slug="going-away", name="Going Away",
        )

    def _graph(agent_id: str) -> dict:
        return {
            "version": 1,
            "nodes": [
                {"id": "n1", "type": "start", "data": {}},
                {"id": "a1", "type": "agent", "data": {"agent_id": agent_id}},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "a1"}],
        }

    with _as_tenant(test_tenant["id"]):
        flow = await call_flows.create_call_flow(
            tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}",
            name="Handoff Flow", graph=_graph(str(live["id"])),
        )

        # Deactivated — parse_graph() sees a perfectly well-formed graph.
        await pool.execute("UPDATE agents SET status = 'inactive' WHERE id = $1", going_away["id"])
        with pytest.raises(call_flows.CallFlowValidationError) as exc:
            await call_flows.publish(flow["id"], _graph(str(going_away["id"])))
        assert [(e.kind, e.id, e.field) for e in exc.value.errors] == [("node", "a1", "agent_id")]

        # Another tenant's agent.
        with _as_tenant(other_tenant["id"]):
            outsider = await agents.create_agent(
                tenant_id=other_tenant["id"], slug="outsider", name="Outsider",
            )
        with pytest.raises(call_flows.CallFlowValidationError):
            await call_flows.publish(flow["id"], _graph(str(outsider["id"])))

        # Gone entirely.
        with pytest.raises(call_flows.CallFlowValidationError):
            await call_flows.publish(flow["id"], _graph(str(uuid.uuid4())))

        # Not a uuid at all — must be a red node, never a 500 from `::uuid[]`.
        with pytest.raises(call_flows.CallFlowValidationError):
            await call_flows.publish(flow["id"], _graph("not-a-uuid"))

        # uuid.UUID() accepts these, but asyncpg rejects braces/urn and uppercase misses the
        # runtime's exact-string agent_slugs lookup.
        live_id = str(live["id"])
        for variant in (f"{{{live_id}}}", f"urn:uuid:{live_id}", live_id.upper()):
            with pytest.raises(call_flows.CallFlowValidationError) as exc:
                await call_flows.publish(flow["id"], _graph(variant))
            assert [(e.kind, e.id, e.field) for e in exc.value.errors] == [("node", "a1", "agent_id")], variant

        # The live agent still publishes, and nothing above left a version behind.
        published = await call_flows.publish(flow["id"], _graph(str(live["id"])))
    assert published["config_version"] == 2, "the refused publishes must not have bumped it"

    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other_tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


async def test_agent_deactivation_or_delete_reaches_a_warm_runtime_cache(test_tenant, pool):
    """A deactivated or deleted agent drops out of a warm cached payload on the next read."""
    with _as_tenant(test_tenant["id"]):
        deactivated = await agents.create_agent(
            tenant_id=test_tenant["id"], slug="to-deactivate", name="To Deactivate",
        )
        deleted = await agents.create_agent(
            tenant_id=test_tenant["id"], slug="to-delete", name="To Delete",
        )
    graph = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "start", "data": {}},
            {"id": "m1", "type": "menu", "data": {"prompt": "Pick one."}},
            {"id": "a1", "type": "agent", "data": {"agent_id": str(deactivated["id"])}},
            {"id": "a2", "type": "agent", "data": {"agent_id": str(deleted["id"])}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "m1"},
            {"id": "e2", "source": "m1", "target": "a1", "data": {"key": "1"}},
            {"id": "e3", "source": "m1", "target": "a2", "data": {"key": "2"}},
        ],
    }
    with _as_tenant(test_tenant["id"]):
        flow = await call_flows.create_call_flow(
            tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}", name="Warm Flow", graph=graph,
        )
        assert flow["graph"] is not None

        warm = await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"])
        assert set(warm["agent_slugs"]) == {str(deactivated["id"]), str(deleted["id"])}
        key = call_flows._runtime_cache_key(test_tenant["slug"], flow["id"])
        assert await cache.get_json(key) is not None, "the payload must be cached for this test to mean anything"

        await agents.update_agent(deactivated["id"], tenant_slug=test_tenant["slug"], status="inactive")
        after_deactivate = await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"])
        assert set(after_deactivate["agent_slugs"]) == {str(deleted["id"])}

        await agents.soft_delete_agent(deleted["id"], tenant_slug=test_tenant["slug"])
        after_delete = await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"])
        assert after_delete["agent_slugs"] == {}
