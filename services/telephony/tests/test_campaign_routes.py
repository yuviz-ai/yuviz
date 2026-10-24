"""Campaign-facing Telephony routes: reconcile, call status, outbound readiness and the 429 body on /call."""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid

import pytest
from fastapi.testclient import TestClient

from libs.telephony_sdk.providers.cloudonix import CloudonixProvider
from libs.telephony_sdk.providers.fake import FakeProvider
from services.config.auth import create_access_token
from services.telephony import ownership as ownership_module
from services.telephony.accounts import Account, accounts
from services.telephony.app import app
from services.telephony.auth import require_telephony_caller

TENANT_A = str(uuid.uuid4())


def _headers() -> dict[str, str]:
    token = create_access_token({
        "id": str(uuid.uuid4()), "email": "svc@example.test", "role": "viewer",
        "tenant_id": None, "is_service_account": True,
    })
    return {"Authorization": f"Bearer {token}"}


def _wire(instance, provider: str = "fake") -> None:
    account = Account(
        provider=provider, account_ref="cfg-a", tenant_id=TENANT_A, tenant_slug="tenant-a",
        is_default_outbound=True, credentials={}, instance=instance,
    )
    accounts._accounts = {(provider, "cfg-a"): account}
    accounts._default_outbound = {"tenant-a": account}
    accounts._agent_memo = {("tenant-a", "sales"): True}
    accounts._loaded = True


@pytest.fixture(autouse=True)
def _env(monkeypatch, _scratch_redis_port):
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{_scratch_redis_port}/0")

    async def _resolve(did: str):
        return {"1555": ("tenant-a", "sales")}.get(did)

    async def _noop_refresh(self):
        return None

    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _resolve)
    monkeypatch.setattr(accounts.__class__, "refresh", _noop_refresh)
    _wire(FakeProvider({}))
    yield
    accounts._accounts = {}
    accounts._default_outbound = {}
    accounts._agent_memo = {}
    accounts._loaded = False


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_reconcile_route_against_the_fake_provider(client):
    _wire(FakeProvider({"reconcile_outcome": "placed"}))
    resp = client.post("/fake/call/reconcile", json={"tenant_slug": "tenant-a", "idempotency_key": "k_1"}, headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "placed", "provider_call_id": "fake-call-k_1"}

    _wire(FakeProvider({"reconcile_outcome": "indeterminate"}))
    resp = client.post("/fake/call/reconcile", json={"tenant_slug": "tenant-a", "idempotency_key": "k_1"}, headers=_headers())
    assert resp.json() == {"outcome": "unknown", "provider_call_id": None}


def test_reconcile_rejects_a_malformed_key(client):
    resp = client.post("/fake/call/reconcile", json={"tenant_slug": "tenant-a", "idempotency_key": "a b/c"}, headers=_headers())
    assert resp.status_code == 422


def test_status_route_maps_state_and_returns_unknown_for_a_foreign_call(client):
    _wire(FakeProvider({"call_statuses": {"c1": {"status": "no-answer", "to": "+919876543210", "from": "+918065354620"}}}))
    resp = client.get("/fake/call/c1/status", params={"tenant_slug": "tenant-a"}, headers=_headers())
    assert resp.json() == {"status": "no_answer", "to": "+919876543210", "from": "+918065354620"}

    resp = client.get("/fake/call/another-tenants-call/status", params={"tenant_slug": "tenant-a"}, headers=_headers())
    assert resp.status_code == 200 and resp.json()["status"] == "unknown"


def test_status_route_refuses_a_call_id_that_could_alter_the_provider_path(client):
    resp = client.get("/fake/call/..%2F..%2Fadmin/status", params={"tenant_slug": "tenant-a"}, headers=_headers())
    assert resp.status_code in (404, 422)


def test_readiness_is_true_for_the_fake_and_false_for_cloudonix(client):
    resp = client.get("/fake/outbound-readiness", params={"tenant_slug": "tenant-a"}, headers=_headers())
    assert resp.json() == {"ready": True, "reason": None}

    _wire(FakeProvider({"check_health_result": False}))
    resp = client.get("/fake/outbound-readiness", params={"tenant_slug": "tenant-a"}, headers=_headers())
    assert resp.json() == {"ready": False, "reason": "provider_unhealthy"}

    _wire(CloudonixProvider({"domain": "d.example", "api_keys": ["k"]}), provider="cloudonix")
    resp = client.get("/cloudonix/outbound-readiness", params={"tenant_slug": "tenant-a"}, headers=_headers())
    assert resp.json() == {"ready": False, "reason": "provider_not_campaign_capable"}


def test_readiness_without_an_outbound_account_is_not_ready(client):
    accounts._default_outbound = {}
    resp = client.get("/fake/outbound-readiness", params={"tenant_slug": "tenant-a"}, headers=_headers())
    assert resp.json() == {"ready": False, "reason": "no_default_outbound_account"}


def test_new_routes_refuse_unauthenticated_and_foreign_tenant_callers(client):
    assert client.get("/fake/outbound-readiness", params={"tenant_slug": "tenant-a"}).status_code == 401
    foreign = create_access_token({
        "id": str(uuid.uuid4()), "email": "a@example.test", "role": "admin",
        "tenant_id": str(uuid.uuid4()), "is_service_account": False,
    })
    resp = client.get("/fake/call/c1/status", params={"tenant_slug": "tenant-a"}, headers={"Authorization": f"Bearer {foreign}"})
    assert resp.status_code == 404


def test_rate_limited_call_returns_429_with_the_retry_after_body(client):
    _wire(FakeProvider({"initiate_rate_limited_calls": 1}))
    body = {"tenant_slug": "tenant-a", "agent_slug": "sales", "to": "9", "from": "1555", "idempotency_key": f"k_{uuid.uuid4().hex}"}
    resp = client.post("/fake/call", json=body, headers=_headers())
    assert resp.status_code == 429
    assert resp.json() == {"error": "rate_limited", "retry_after": 7}
    assert client.post("/fake/call", json=body, headers=_headers()).status_code == 200


def test_every_caller_gated_route_awaits_resolve_caller_tenant():
    handlers = set()
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is not None and any(dep.call is require_telephony_caller for dep in dependant.dependencies):
            handlers.add(route.endpoint)
    assert len(handlers) == 6
    for handler in handlers:
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        awaited = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "attr", getattr(n.value.func, "id", None)) == "resolve_caller_tenant"
        ]
        assert awaited, f"{handler.__name__} does not await resolve_caller_tenant"
