"""Capacity routes: operator-only gate, strict validation, atomic audit, write serialization and usage counts."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from fastapi import Header

from libs.tenancy import platform_conn, set_caller_tenant
from services.campaigns import app as app_module, capacity
from services.campaigns.routers.capacity import capacity_router
from services.campaigns.tests import seed
from services.config.auth import CurrentUser
from services.config.deps import get_current_user

TRUNK = "sip_trunk:pstn"
BASELINE = (300, 75, 225)
LIMIT_FIELDS = ("total_call_capacity", "inbound_reserve", "max_outbound_concurrent_calls")
OPERATOR = {"x-role": "superadmin"}
PLATFORM_BODY = {"total_call_capacity": 100, "inbound_reserve": 10, "max_outbound_concurrent_calls": 50}


@pytest.fixture
async def api(scratch_pool):
    user_id = str(await scratch_pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", f"{uuid.uuid4()}@example.test",
    ))

    async def fake_user(x_role: str = Header(), x_tenant: str | None = Header(default=None)):
        set_caller_tenant(x_tenant)
        return CurrentUser(id=user_id, email="op@example.test", role=x_role, tenant_id=x_tenant)

    app = app_module.app
    app.dependency_overrides[get_current_user] = fake_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.user_id = user_id
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def _clean(scratch_pool):
    await scratch_pool.execute("DELETE FROM campaign_attempts")
    await scratch_pool.execute("DELETE FROM outbound_cps_limits")
    await _reset_platform(scratch_pool)


async def _reset_platform(pool):
    await pool.execute(
        "UPDATE platform_outbound_limits SET total_call_capacity = $1, inbound_reserve = $2, "
        "max_outbound_concurrent_calls = $3, updated_by = NULL", *BASELINE,
    )


async def _tenant(pool, cap=None):
    async with pool.acquire() as conn:
        return str(await seed.tenant(conn, outbound_cap=cap))


async def _snapshot(pool):
    return [
        [dict(r) for r in await pool.fetch(sql)]
        for sql in (
            "SELECT * FROM platform_outbound_limits",
            "SELECT * FROM outbound_cps_limits ORDER BY scope",
            "SELECT id, max_outbound_concurrent_calls FROM tenants ORDER BY id",
            "SELECT count(*) FROM audit_log",
        )
    ]


async def _audit(pool, entity_type, after_id=0):
    rows = await pool.fetch(
        "SELECT * FROM audit_log WHERE entity_type = $1 AND id > $2 ORDER BY id", entity_type, after_id,
    )
    return [dict(r) for r in rows]


async def _last_audit_id(pool):
    return await pool.fetchval("SELECT coalesce(max(id), 0) FROM audit_log")


def _routes(tenant_id):
    return [
        ("GET", "/capacity/platform", None),
        ("PUT", "/capacity/platform", PLATFORM_BODY),
        ("PUT", f"/capacity/cps/{TRUNK}", {"cps": 5}),
        ("GET", "/capacity/usage", None),
        ("GET", "/capacity/tenants", None),
        ("GET", f"/capacity/tenants/{tenant_id}", None),
        ("PUT", f"/capacity/tenants/{tenant_id}", {"max_outbound_concurrent_calls": 3}),
    ]


def test_the_router_has_exactly_seven_routes():
    assert len(capacity_router.routes) == 7


async def test_every_route_is_walked_by_the_gate_test(scratch_pool):
    tenant_id = await _tenant(scratch_pool)
    walked = {(m, p.replace(tenant_id, "{tenant_id}").replace(TRUNK, "{scope}")) for m, p, _ in _routes(tenant_id)}
    declared = {(m, r.path) for r in capacity_router.routes for m in r.methods}
    assert len(walked) == 7
    assert walked == declared


NON_OPERATORS = {
    "admin": lambda t: {"x-role": "admin", "x-tenant": t},
    "viewer": lambda t: {"x-role": "viewer", "x-tenant": t},
    "service_account": lambda t: {"x-role": "viewer"},
    "tenant_superadmin": lambda t: {"x-role": "superadmin", "x-tenant": t},
}


@pytest.mark.parametrize("principal", NON_OPERATORS)
async def test_non_operators_get_403_on_every_route_and_change_nothing(scratch_pool, api, principal):
    tenant_a, tenant_b = await _tenant(scratch_pool), await _tenant(scratch_pool)
    headers = NON_OPERATORS[principal](tenant_a)
    before = await _snapshot(scratch_pool)
    for ids in (tenant_a, tenant_b, str(uuid.uuid4())):
        for method, path, body in _routes(ids):
            resp = await api.request(method, path, json=body, headers=headers)
            assert resp.status_code == 403, (method, path, resp.text)
    assert await _snapshot(scratch_pool) == before


@pytest.mark.parametrize("principal", NON_OPERATORS)
async def test_a_non_operator_sees_one_response_for_own_other_and_random_tenant_ids(scratch_pool, api, principal):
    tenant_a, tenant_b = await _tenant(scratch_pool), await _tenant(scratch_pool)
    headers = NON_OPERATORS[principal](tenant_a)
    for method, body in (("GET", None), ("PUT", {"max_outbound_concurrent_calls": 3})):
        seen = set()
        for ids in (tenant_a, tenant_b, str(uuid.uuid4()), "not-a-uuid"):
            resp = await api.request(method, f"/capacity/tenants/{ids}", json=body, headers=headers)
            seen.add((resp.status_code, resp.text))
        assert len(seen) == 1


async def test_the_operator_reads_platform_limits_with_cps_scopes(scratch_pool, api):
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 7)", TRUNK)
    resp = await api.get("/capacity/platform", headers=OPERATOR)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["total_call_capacity"], body["inbound_reserve"], body["max_outbound_concurrent_calls"]) == BASELINE
    assert [(c["scope"], c["cps"]) for c in body["cps_limits"]] == [(TRUNK, 7)]
    assert {"updated_at", "updated_by"} <= body.keys()


async def test_a_platform_update_stores_values_stamps_updated_by_and_writes_one_exact_audit_row(scratch_pool, api):
    mark = await _last_audit_id(scratch_pool)
    resp = await api.put("/capacity/platform", json=PLATFORM_BODY, headers=OPERATOR)
    assert resp.status_code == 200, resp.text
    row = await scratch_pool.fetchrow("SELECT * FROM platform_outbound_limits")
    assert tuple(row[f] for f in LIMIT_FIELDS) == (100, 10, 50)
    assert str(row["updated_by"]) == api.user_id == resp.json()["updated_by"]
    (entry,) = await _audit(scratch_pool, "platform_outbound_limits", mark)
    assert str(entry["user_id"]) == api.user_id and entry["action"] == "updated"
    old, new = json.loads(entry["old_value"]), json.loads(entry["new_value"])
    assert tuple(old[f] for f in LIMIT_FIELDS) == BASELINE
    assert tuple(new[f] for f in LIMIT_FIELDS) == (100, 10, 50)


async def test_a_null_platform_cap_round_trips(scratch_pool, api):
    body = {"total_call_capacity": None, "inbound_reserve": 0, "max_outbound_concurrent_calls": None}
    assert (await api.put("/capacity/platform", json=body, headers=OPERATOR)).status_code == 200
    got = (await api.get("/capacity/platform", headers=OPERATOR)).json()
    assert (got["total_call_capacity"], got["max_outbound_concurrent_calls"]) == (None, None)


async def test_an_over_capacity_cap_is_400_with_the_row_unchanged_and_no_audit(scratch_pool, api):
    before = await _snapshot(scratch_pool)
    body = {"total_call_capacity": 100, "inbound_reserve": 10, "max_outbound_concurrent_calls": 91}
    resp = await api.put("/capacity/platform", json=body, headers=OPERATOR)
    assert resp.status_code == 400
    assert resp.json() == {"detail": "outbound cap exceeds capacity minus inbound reserve"}
    assert await _snapshot(scratch_pool) == before


def _mutated(base, field, value):
    return {k: v for k, v in base.items() if k != field} if value is capacity else {**base, field: value}


MISSING = capacity
PLATFORM_BAD = [
    (f, v) for f, v in (
        ("total_call_capacity", 0), ("total_call_capacity", -1), ("total_call_capacity", "5"),
        ("total_call_capacity", 5.5), ("total_call_capacity", True), ("total_call_capacity", MISSING),
        ("inbound_reserve", -1), ("inbound_reserve", "5"), ("inbound_reserve", 5.5),
        ("inbound_reserve", True), ("inbound_reserve", None), ("inbound_reserve", MISSING),
        ("max_outbound_concurrent_calls", 0), ("max_outbound_concurrent_calls", -1),
        ("max_outbound_concurrent_calls", "5"), ("max_outbound_concurrent_calls", 5.5),
        ("max_outbound_concurrent_calls", True), ("max_outbound_concurrent_calls", MISSING),
    )
]
CPS_BAD = [0, -1, "5", 5.5, True, MISSING]
TENANT_BAD = [0, -1, "5", 5.5, True, MISSING]


async def test_bad_platform_values_are_400_and_change_nothing(scratch_pool, api):
    before = await _snapshot(scratch_pool)
    for field, value in PLATFORM_BAD:
        resp = await api.put("/capacity/platform", json=_mutated(PLATFORM_BODY, field, value), headers=OPERATOR)
        assert resp.status_code == 400, (field, value, resp.text)
        detail = resp.json()["detail"]
        assert isinstance(detail, str) or all(set(e) == {"loc", "msg"} for e in detail), (field, value, detail)
    assert await _snapshot(scratch_pool) == before


async def test_bad_cps_values_are_400_and_change_nothing(scratch_pool, api):
    before = await _snapshot(scratch_pool)
    for value in CPS_BAD:
        resp = await api.put(f"/capacity/cps/{TRUNK}", json=_mutated({"cps": 5}, "cps", value), headers=OPERATOR)
        assert resp.status_code == 400, (value, resp.text)
    assert await _snapshot(scratch_pool) == before


async def test_bad_tenant_cap_values_are_400_and_change_nothing(scratch_pool, api):
    tenant_id = await _tenant(scratch_pool, cap=4)
    before = await _snapshot(scratch_pool)
    field = "max_outbound_concurrent_calls"
    for value in TENANT_BAD:
        resp = await api.put(f"/capacity/tenants/{tenant_id}", json=_mutated({field: 3}, field, value), headers=OPERATOR)
        assert resp.status_code == 400, (value, resp.text)
    assert await _snapshot(scratch_pool) == before


async def test_a_malformed_json_body_is_400(scratch_pool, api):
    resp = await api.put(
        "/capacity/platform", content=b"{not json", headers={**OPERATOR, "content-type": "application/json"},
    )
    assert resp.status_code == 400


async def test_a_validation_error_does_not_echo_the_submitted_value(scratch_pool, api):
    resp = await api.put("/capacity/cps/" + TRUNK, json={"cps": "secret-marker"}, headers=OPERATOR)
    assert resp.status_code == 400
    assert "secret-marker" not in resp.text


async def test_cps_write_inserts_then_updates_with_exact_audit_old_values(scratch_pool, api):
    mark = await _last_audit_id(scratch_pool)
    for cps in (5, 8):
        resp = await api.put(f"/capacity/cps/{TRUNK}", json={"cps": cps}, headers=OPERATOR)
        assert resp.status_code == 200, resp.text
        assert (resp.json()["scope"], resp.json()["cps"]) == (TRUNK, cps)
    first, second = await _audit(scratch_pool, "outbound_cps_limit", mark)
    assert (first["action"], first["old_value"], json.loads(first["new_value"])["cps"]) == ("created", None, 5)
    assert (second["action"], json.loads(second["old_value"])["cps"], json.loads(second["new_value"])["cps"]) == ("updated", 5, 8)
    assert {str(first["user_id"]), str(second["user_id"])} == {api.user_id}
    row = await scratch_pool.fetchrow("SELECT * FROM outbound_cps_limits WHERE scope = $1", TRUNK)
    assert str(row["updated_by"]) == api.user_id


async def test_an_unknown_cps_scope_is_400_with_no_row_and_no_audit(scratch_pool, api):
    before = await _snapshot(scratch_pool)
    resp = await api.put("/capacity/cps/bogus", json={"cps": 5}, headers=OPERATOR)
    assert resp.status_code == 400
    assert resp.json() == {"detail": "unknown CPS scope"}
    assert await _snapshot(scratch_pool) == before


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("tenant_id", [str(uuid.uuid4()), "not-a-uuid"])
async def test_an_unknown_or_non_uuid_tenant_is_404_with_no_audit(scratch_pool, api, method, tenant_id):
    before = await _snapshot(scratch_pool)
    body = {"max_outbound_concurrent_calls": 3} if method == "PUT" else None
    resp = await api.request(method, f"/capacity/tenants/{tenant_id}", json=body, headers=OPERATOR)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "tenant not found"}
    assert await _snapshot(scratch_pool) == before


async def test_a_tenant_cap_round_trips_null_and_audits_each_change(scratch_pool, api):
    tenant_id = await _tenant(scratch_pool, cap=4)
    mark = await _last_audit_id(scratch_pool)
    for cap in (9, None):
        resp = await api.put(f"/capacity/tenants/{tenant_id}", json={"max_outbound_concurrent_calls": cap}, headers=OPERATOR)
        assert resp.status_code == 200, resp.text
        assert resp.json()["max_outbound_concurrent_calls"] == cap
        got = await api.get(f"/capacity/tenants/{tenant_id}", headers=OPERATOR)
        assert got.json() == resp.json() and got.json()["tenant_id"] == tenant_id
    first, second = await _audit(scratch_pool, "tenant_outbound_cap", mark)
    assert str(first["entity_id"]) == tenant_id and str(first["user_id"]) == api.user_id
    assert [json.loads(r["old_value"])["max_outbound_concurrent_calls"] for r in (first, second)] == [4, 9]
    assert [json.loads(r["new_value"])["max_outbound_concurrent_calls"] for r in (first, second)] == [9, None]


async def test_the_tenant_list_carries_caps_and_in_flight_counts(scratch_pool, api):
    tenant_id = await _tenant(scratch_pool, cap=6)
    async with scratch_pool.acquire() as conn:
        camp = await seed.campaign(conn, uuid.UUID(tenant_id))
        for _ in range(2):
            await seed.attempt(conn, camp, await seed.contact(conn, camp["id"]), state="sent")
    rows = {r["tenant_id"]: r for r in (await api.get("/capacity/tenants", headers=OPERATOR)).json()}
    assert (rows[tenant_id]["max_outbound_concurrent_calls"], rows[tenant_id]["in_flight"]) == (6, 2)
    assert {"name", "slug"} <= rows[tenant_id].keys()


async def test_usage_counts_match_the_admission_predicates(scratch_pool, api):
    tenant_id = await _tenant(scratch_pool)
    other_scope = f"provider_account:{uuid.uuid4()}"
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 3)", TRUNK)
    async with scratch_pool.acquire() as conn:
        camp = await seed.campaign(conn, uuid.UUID(tenant_id))

        async def add(**kwargs):
            await seed.attempt(conn, camp, await seed.contact(conn, camp["id"]), **kwargs)

        await add(state="pending")
        await add(state="sent", last_send_at_ago_s=0.1)
        await add(state="sent", last_send_at_ago_s=30)
        await add(state="resolved", outcome="answered", last_send_at_ago_s=0.1)
        await add(state="resolved", outcome="ended", last_send_at_ago_s=0.1)
        await add(state="sent", cps_scope=other_scope, last_send_at_ago_s=0.1)
    body = (await api.get("/capacity/usage", headers=OPERATOR)).json()
    assert body["platform_in_flight"] == 5
    by_scope = {c["scope"]: c for c in body["cps"]}
    assert (by_scope[TRUNK]["cps"], by_scope[TRUNK]["sends_last_second"]) == (3, 3)
    assert (by_scope[other_scope]["cps"], by_scope[other_scope]["sends_last_second"]) == (None, 1)


async def test_a_configured_scope_with_no_sends_is_reported_with_zero(scratch_pool, api):
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 3)", TRUNK)
    body = (await api.get("/capacity/usage", headers=OPERATOR)).json()
    assert body["cps"] == [{"scope": TRUNK, "cps": 3, "sends_last_second": 0}]


async def _race_once(pool, api):
    """One pair of concurrent platform writes; True when the end state and audit chain are consistent."""
    await _reset_platform(pool)
    mark = await _last_audit_id(pool)
    a = {"total_call_capacity": 100, "inbound_reserve": 10, "max_outbound_concurrent_calls": 50}
    b = {"total_call_capacity": 200, "inbound_reserve": 20, "max_outbound_concurrent_calls": 60}
    resps = await asyncio.gather(*(api.put("/capacity/platform", json=x, headers=OPERATOR) for x in (a, b)))
    assert [r.status_code for r in resps] == [200, 200]
    row = await pool.fetchrow("SELECT * FROM platform_outbound_limits")
    final = {f: row[f] for f in LIMIT_FIELDS}
    first, second = await _audit(pool, "platform_outbound_limits", mark)
    pick = lambda blob: {f: json.loads(blob)[f] for f in LIMIT_FIELDS}  # noqa: E731
    return (
        final in (a, b)
        and pick(first["old_value"]) == dict(zip(LIMIT_FIELDS, BASELINE))
        and pick(second["old_value"]) == pick(first["new_value"])
        and pick(second["new_value"]) == final
    )


async def test_concurrent_platform_writes_leave_one_requests_values_and_a_linear_audit_chain(scratch_pool, api):
    results = [await _race_once(scratch_pool, api) for _ in range(50)]
    assert all(results)


async def test_the_race_test_fails_when_the_for_update_lock_is_removed(scratch_pool, api, monkeypatch):
    real = capacity.get_platform_limits

    async def unlocked(conn, *, for_update=False):
        return await real(conn)

    monkeypatch.setattr(capacity, "get_platform_limits", unlocked)
    results = [await _race_once(scratch_pool, api) for _ in range(50)]
    assert not all(results)


async def test_admission_completes_while_a_capacity_write_holds_the_lock(scratch_pool):
    tenant_id = await _tenant(scratch_pool)
    async with scratch_pool.acquire() as conn:
        camp = await seed.campaign(conn, uuid.UUID(tenant_id))
    async with platform_conn(scratch_pool, reason="test") as writer:
        await capacity.get_platform_limits(writer, for_update=True)
        await writer.execute("UPDATE platform_outbound_limits SET inbound_reserve = inbound_reserve")
        async with platform_conn(scratch_pool, reason="test") as reader:
            assert await asyncio.wait_for(capacity.admit(reader, camp, TRUNK), timeout=5) is None
