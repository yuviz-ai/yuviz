"""Finding #5: a callback validly signed by account A carrying tenant B's
idempotency key must write idemref only under A's tenant, and B's own
observed_call_id() must still return None — a route with no account_ref
segment would have no tenant to scope the write to."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import idempotency
from services.telephony.accounts import Account, accounts
from services.telephony.app import app

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())


class _AlwaysValidFake(FakeProvider):
    def verify_webhook_signature(self, url, headers):
        return True


@pytest.fixture
def client(monkeypatch):
    fake_a = _AlwaysValidFake({})
    accounts._accounts = {
        ("fake", "cfg-a"): Account(
            provider="fake", account_ref="cfg-a", tenant_id=TENANT_A, tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=fake_a,
        ),
    }
    accounts._loaded = True

    async def _noop_refresh(self):
        return None

    async def _noop_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(accounts.__class__, "refresh", _noop_refresh)
    import services.telephony.app as app_module
    monkeypatch.setattr(app_module, "_notify_campaigns_best_effort", _noop_notify)

    with TestClient(app) as c:
        yield c
    accounts._accounts = {}
    accounts._loaded = False


@pytest.mark.asyncio
async def test_callback_writes_idemref_only_under_signing_accounts_tenant(client):
    tenant_b_key = "tenant-b-minted-key"

    resp = client.post(
        "/fake/status/cfg-a", params={"idem": tenant_b_key}, data={"call_uuid": "vendor-call-1"},
    )
    assert resp.status_code == 200

    # The redis client the app just used was created inside TestClient's own
    # worker loop; force a fresh one bound to this test's loop before reading.
    idempotency._client = None
    observed_a = await idempotency.observed_call_id("fake", uuid.UUID(TENANT_A), tenant_b_key)
    observed_b = await idempotency.observed_call_id("fake", uuid.UUID(TENANT_B), tenant_b_key)
    assert observed_a == "vendor-call-1"
    assert observed_b is None


def test_status_route_requires_account_ref_segment():
    matched = [r for r in app.routes if getattr(r, "path", "") == "/{provider}/status/{account_ref}"]
    assert len(matched) == 1


@pytest.mark.asyncio
async def test_ring_event_does_not_notify_campaigns_only_hangup_does(client, monkeypatch):
    calls = []

    async def _record(*args, **kwargs):
        calls.append(args)

    import services.telephony.app as app_module
    monkeypatch.setattr(app_module, "_notify_campaigns_best_effort", _record)

    client.post("/fake/status/cfg-a", params={"idem": "k1", "event": "ring"}, data={"call_uuid": "c1"})
    assert calls == []

    client.post("/fake/status/cfg-a", params={"idem": "k1", "event": "hangup"}, data={"call_uuid": "c1"})
    assert len(calls) == 1
