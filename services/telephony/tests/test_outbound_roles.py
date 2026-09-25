"""R2-2: a tenant `viewer` must never reach an outbound route, while a
NULL-tenant `is_service_account` token carrying the SAME role="viewer"
claim must — distinguishing the two clauses rather than passing on the
role alone (lesson 24)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from libs.telephony_sdk.providers.fake import FakeProvider
from services.config.auth import create_access_token
from services.telephony import ownership as ownership_module
from services.telephony.accounts import Account, accounts
from services.telephony.app import app

TENANT_A = str(uuid.uuid4())


def _token(*, role: str, tenant_id: str | None, is_service_account: bool = False) -> str:
    return create_access_token({
        "id": str(uuid.uuid4()), "email": "u@example.test", "role": role,
        "tenant_id": tenant_id, "is_service_account": is_service_account,
    })


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    fake = FakeProvider({})
    accounts._accounts = {
        ("fake", "cfg-a"): Account(
            provider="fake", account_ref="cfg-a", tenant_id=TENANT_A, tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=fake,
        ),
    }
    accounts._default_outbound = {"tenant-a": accounts._accounts[("fake", "cfg-a")]}
    accounts._agent_memo = {("tenant-a", "sales"): True}
    accounts._loaded = True

    async def _resolve(did: str):
        return {"1555": ("tenant-a", "sales")}.get(did)

    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _resolve)

    async def _noop_refresh(self):
        return None

    monkeypatch.setattr(accounts.__class__, "refresh", _noop_refresh)
    yield fake
    accounts._accounts = {}
    accounts._default_outbound = {}
    accounts._agent_memo = {}
    accounts._loaded = False


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _place_call_body(**overrides):
    body = {"tenant_slug": "tenant-a", "agent_slug": "sales", "to": "9", "from": "1555", "idempotency_key": "k"}
    body.update(overrides)
    return body


def test_tenant_viewer_is_403(client, _wire):
    token = _token(role="viewer", tenant_id=TENANT_A)
    resp = client.post("/fake/call", json=_place_call_body(idempotency_key="v1"), headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    resp2 = client.post(
        "/sms/send",
        json={"tenant_slug": "tenant-a", "to": "9", "from": "1555", "text": "hi", "idempotency_key": "v2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 403
    assert _wire.dial_count == 0
    assert _wire.send_count == 0


def test_null_tenant_service_account_same_role_viewer_passes(client, _wire):
    token = _token(role="viewer", tenant_id=None, is_service_account=True)
    resp = client.post("/fake/call", json=_place_call_body(idempotency_key="s1"), headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert _wire.dial_count == 1


def test_tenant_scoped_service_account_is_still_refused(client, _wire):
    token = _token(role="viewer", tenant_id=TENANT_A, is_service_account=True)
    resp = client.post("/fake/call", json=_place_call_body(idempotency_key="s2"), headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert _wire.dial_count == 0


def test_route_enumeration_size(monkeypatch):
    from services.telephony.auth import require_telephony_caller

    paths = set()
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        if any(dep.call is require_telephony_caller for dep in dependant.dependencies):
            paths.add(route.path)
    assert len(paths) == 3
