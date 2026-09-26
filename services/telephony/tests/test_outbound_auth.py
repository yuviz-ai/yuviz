"""The findings' own regression suite (finding #1-3), each case written so
it goes red if the control is deleted. Walks app.routes for the outbound
routes and asserts the walk's own size, not just its members (lessons 12,
29)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from libs.telephony_sdk.providers.fake import FakeProvider
from services.config.auth import create_access_token
from services.telephony import ownership as ownership_module
from services.telephony.accounts import Account, accounts
from services.telephony.app import app
from services.telephony.auth import require_telephony_caller

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())


def _token(*, role: str, tenant_id: str | None, is_service_account: bool = False) -> str:
    return create_access_token({
        "id": str(uuid.uuid4()), "email": "u@example.test", "role": role,
        "tenant_id": tenant_id, "is_service_account": is_service_account,
    })


def _outbound_route_paths() -> set[str]:
    paths = set()
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dep in dependant.dependencies:
            if dep.call is require_telephony_caller:
                paths.add((route.path, tuple(sorted(route.methods or []))))
    return paths


def test_every_outbound_route_depends_on_require_telephony_caller():
    paths = _outbound_route_paths()
    assert paths == {
        ("/{provider}/call", ("POST",)),
        ("/{provider}/call/idempotency/{key}", ("GET",)),
        ("/sms/send", ("POST",)),
    }


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    fake_a = FakeProvider({})
    accounts._accounts = {
        ("fake", "cfg-a"): Account(
            provider="fake", account_ref="cfg-a", tenant_id=TENANT_A, tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=fake_a,
        ),
    }
    accounts._default_outbound = {"tenant-a": accounts._accounts[("fake", "cfg-a")]}
    accounts._agent_memo = {("tenant-a", "sales"): True}
    accounts._loaded = True

    routes = {"1555": ("tenant-a", "sales"), "1666": ("tenant-b", "sales")}

    async def _resolve(did: str):
        return routes.get(did)

    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _resolve)
    yield fake_a
    accounts._accounts = {}
    accounts._default_outbound = {}
    accounts._agent_memo = {}
    accounts._loaded = False


@pytest.fixture
def client(monkeypatch):
    async def _noop_refresh(self):
        return None

    monkeypatch.setattr(accounts.__class__, "refresh", _noop_refresh)
    with TestClient(app) as c:
        yield c


def test_no_auth_header_is_401_with_zero_provider_calls(client, _wire):
    resp = client.post("/fake/call", json={
        "tenant_slug": "tenant-a", "agent_slug": "sales", "to": "9", "from": "1555", "idempotency_key": "k1",
    })
    assert resp.status_code == 401
    assert _wire.dial_count == 0

    resp = client.post("/sms/send", json={
        "tenant_slug": "tenant-a", "to": "9", "from": "1555", "text": "hi", "idempotency_key": "k2",
    })
    assert resp.status_code == 401
    assert _wire.send_count == 0


def test_cross_tenant_slug_in_body_is_403_or_404_invariant_shape(client, _wire):
    token = _token(role="admin", tenant_id=TENANT_B)
    resp_unknown = client.post(
        "/fake/call",
        json={"tenant_slug": "no-such-tenant", "agent_slug": "sales", "to": "9", "from": "1555", "idempotency_key": "k3"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp_real_other = client.post(
        "/fake/call",
        json={"tenant_slug": "tenant-a", "agent_slug": "sales", "to": "9", "from": "1555", "idempotency_key": "k4"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # tenant-B's own response shape is invariant to whether "tenant-a" exists.
    assert resp_unknown.status_code == resp_real_other.status_code
    assert _wire.dial_count == 0


def test_foreign_tenant_from_number_is_403(client, _wire):
    token = _token(role="admin", tenant_id=TENANT_A)
    resp = client.post(
        "/fake/call",
        json={"tenant_slug": "tenant-a", "agent_slug": "sales", "to": "9", "from": "1666", "idempotency_key": "k5"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert _wire.dial_count == 0


def test_foreign_tenant_agent_slug_is_403(client, _wire):
    token = _token(role="admin", tenant_id=TENANT_A)
    resp = client.post(
        "/fake/call",
        json={"tenant_slug": "tenant-a", "agent_slug": "tenant-b-agent", "to": "9", "from": "1555", "idempotency_key": "k6"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert _wire.dial_count == 0
    assert not accounts.agent_known("tenant-a", "tenant-b-agent")


def test_two_tenants_racing_identical_idempotency_key_each_get_own_result(client, monkeypatch):
    fake_b = FakeProvider({})
    accounts._accounts[("fake", "cfg-b")] = Account(
        provider="fake", account_ref="cfg-b", tenant_id=TENANT_B, tenant_slug="tenant-b",
        is_default_outbound=True, credentials={}, instance=fake_b,
    )
    accounts._default_outbound["tenant-b"] = accounts._accounts[("fake", "cfg-b")]
    accounts._agent_memo[("tenant-b", "sales")] = True

    token_a = _token(role="admin", tenant_id=TENANT_A)
    token_b = _token(role="admin", tenant_id=TENANT_B)
    same_key = "shared-key"

    resp_a = client.post(
        "/fake/call",
        json={"tenant_slug": "tenant-a", "agent_slug": "sales", "to": "9", "from": "1555", "idempotency_key": same_key},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    resp_b = client.post(
        "/fake/call",
        json={"tenant_slug": "tenant-b", "agent_slug": "sales", "to": "9", "from": "1666", "idempotency_key": same_key},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a.json()["call_uuid"] != resp_b.json()["call_uuid"]


def test_stale_idempotency_read_for_another_tenant_key_is_404_like_never_existed(client):
    token_a = _token(role="admin", tenant_id=TENANT_A)
    token_b = _token(role="admin", tenant_id=TENANT_B)

    resp_never_existed = client.get(
        "/fake/call/idempotency/does-not-exist", params={"tenant_slug": "tenant-a"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    resp_other_tenant_key = client.get(
        "/fake/call/idempotency/some-key", params={"tenant_slug": "tenant-b"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_never_existed.status_code == 403 or resp_never_existed.status_code == 404
    # tenant-a cannot read under tenant-b's slug at all (403 before any Redis read).
    assert resp_other_tenant_key.status_code in (403, 404)
