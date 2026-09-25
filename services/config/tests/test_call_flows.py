"""Call-flow (IVR) service + the runtime /published route, against real
Postgres + Redis. Mirrors test_workflows.py's shape for the service-layer
CRUD, and test_live_calls.py's _client_as()/ASGITransport shape for the
route-level RLS assertions."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.tenancy import current_scope, set_caller_tenant, set_target_tenant
from services.config import agents, auth, cache, call_flows, provider_configs
from services.config.app import app


@contextmanager
def _as_tenant(tenant_id):
    """Direct service-layer calls (not through the app) have no ambient RLS
    scope the way a real request does (deps.get_authenticated_user/
    bind_path_tenant) — stand in for that here, the same two GUCs a request
    would set."""
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
    """call_flows.tenant_id has no ON DELETE clause (plain RESTRICT), unlike
    agents/provider_configs — test_tenant's own teardown would fail with a
    foreign-key violation if a flow created here outlived it."""
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
    """The conversation service account: tenant_id IS NULL, role='viewer' —
    the platform-scoped-but-not-superadmin shape (lesson 24). A real row,
    not just a signed token: get_current_user() re-reads it
    (fresh_console_authority), so a fabricated id with no backing row now
    401s before the route ever runs."""
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
    await pool.execute("UPDATE agents SET status = 'inactive' WHERE id = $1", inactive_agent["id"])
    other_tenant_agent_id = str(uuid.uuid4())
    # Same-id-shaped row planted in the OTHER tenant — a query that resolves
    # agents without the tenant scope would leak this one in (lesson 29/30
    # shape: reproduce the leak, don't just assert its absence).
    await pool.execute(
        "INSERT INTO agents (id, tenant_id, slug, name) VALUES ($1, $2, 'planted', 'Planted')",
        other_tenant_agent_id, other_tenant["id"],
    )

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
    with _as_tenant(test_tenant["id"]):
        flow = await call_flows.create_call_flow(
            tenant_id=test_tenant["id"], slug=f"flow-{uuid.uuid4().hex[:6]}", name="Agent Flow", graph=graph,
        )
        payload = await call_flows.get_published_for_runtime(test_tenant["slug"], flow["id"])
    assert payload["agent_slugs"] == {str(same_tenant_agent["id"]): "picked-up"}

    await pool.execute("DELETE FROM agents WHERE id = $1", other_tenant_agent_id)
    await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


async def test_resolved_tts_config_id_null_for_cross_tenant_or_non_tts_provider(test_tenant, pool):
    other_tenant = await _create_tenant(pool)
    with _as_tenant(test_tenant["id"]):
        same_tenant_tts = await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="TTS", role="tts", engine="elevenlabs",
        )
        same_tenant_stt = await provider_configs.create_provider_config(
            tenant_id=test_tenant["id"], name="STT", role="stt", engine="deepgram",
        )
    with _as_tenant(other_tenant["id"]):
        other_tenant_tts = await provider_configs.create_provider_config(
            tenant_id=other_tenant["id"], name="Other TTS", role="tts", engine="elevenlabs",
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
    """Per-caller invariance (lesson 2, sharpened, and narrowed a second time
    on this feature per product decision): for ONE fixed principal (a
    tenant-B admin JWT) and ONE fixed tenant-A slug — the only thing this
    caller supplied — the 404 must not vary with the STATE of the thing
    behind that slug: a real published flow, a random/nonexistent flow id, a
    draft (unpublished) flow, an outbound flow, or a soft-deleted flow. All
    five ids are foreign — different literal call_flow_ids — but since this
    caller is blocked by require_path_tenant_access on the SLUG alone
    (assert_tenant_access's mismatch branch fires before get_published_
    call_flow ever runs, and that branch's detail echoes only the slug,
    which is held fixed here, never the call_flow_id), the flow's own id
    and state have no way to reach the body. Deliberately NOT varying the
    slug itself: a caller-chosen slug echoed back in the 404 (`deps.py`'s
    `f"tenant {tenant!r} not found"`) tells the caller nothing they did not
    already know, so comparing across different self-chosen slugs is not a
    property this test should assert (that was this feature's second
    over-wide invariance assertion; narrowed here rather than left in).
    This still goes red if `assert_tenant_access`'s flow-blind mismatch
    branch is bypassed — e.g. the published route's flow lookup moving
    ahead of `bind_path_tenant`/`require_path_tenant_access`, so a real
    foreign flow starts returning 200 while the random-id/draft/outbound/
    deleted cases stay 404 — or if `assert_tenant_access` itself starts
    resolving the flow and revealing whether it exists via a distinct
    branch (a 403 for one state and a 404 for another, or two differently
    shaped 404s)."""
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
    """`get_published_call_flow`'s 404 detail echoes the caller-supplied
    call_flow_id (`f"call_flow {call_flow_id!r} not found"`) — caller-
    supplied identifiers are not an information oracle (the caller already
    knows the id it sent), so a literal id difference is not the property
    under test here. Strip any UUID-shaped substring before comparing, so
    the assertion is on the message SHAPE (which reason-branch fired),
    not on which id happened to be echoed."""
    body = resp.json()
    detail = body["detail"] if isinstance(body, dict) and "detail" in body else str(body)
    return _UUID_RE.sub("<id>", detail)


async def test_published_route_404_is_invariant_for_service_account_regardless_of_target(
    test_tenant, pool, service_account,
):
    """Same property, for the platform-scoped conversation service account,
    with the tenant SLUG held fixed at test_tenant's own real slug (the
    service account's path segment does not gate it the way it gates an
    ordinary admin — is_platform_scoped() short-circuits assert_tenant_
    access — so the slug is not the interesting variable here; the flow's
    STATE is). Three of the four sub-cases reuse one fixed call_flow_id,
    mutated in place (unpublished -> outbound -> soft-deleted) with an
    explicit cache invalidation between each, so the body is compared
    byte-for-byte with no normalisation needed for those three. The fourth
    (unknown id) necessarily uses a different literal id — there is no way
    to "hold the id fixed" for an id that must not exist — so it is
    compared only after the shared helper strips any UUID-shaped substring
    from the detail text, per the decision that a caller-supplied id is not
    part of the property under test. This still goes red if
    get_published_for_runtime() starts returning a differently-shaped 404
    for one reason than another (e.g. a distinct detail template for
    "wrong direction" vs "no such id"), or if the unknown-id branch stops
    short-circuiting before touching Postgres in a way that changes the
    response's status or shape."""
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
    """The one cross-caller property actually worth comparing (lesson 2):
    both an ordinary tenant admin and the platform-scoped service account
    get 404, never 403, when the target isn't theirs — a 403 here would
    confirm the flow/tenant exists, which a 404-only contract must not do."""
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
