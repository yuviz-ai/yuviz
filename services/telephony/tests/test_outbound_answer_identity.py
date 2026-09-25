"""Regression coverage for review findings #2/#3: the vendor's answer_url
callback for a call THIS service placed must use the outbound leg's real
agent_slug/tenant_slug (remembered at place_call() time), never re-derive
a route via DID lookup against the callee's number — which previously (a)
silently downgraded the answering agent to "default", and (b) 403'd with
dead air whenever the dialled number happened to be provisioned as
another tenant's own DID."""

from __future__ import annotations

import uuid

import pytest
from starlette.requests import Request

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import orchestrator, outbound
from services.telephony.accounts import Account, accounts
from services.telephony.callctx import outbound_identities
from services.telephony.ownership import OutboundIdentity


def _make_request(*, path: str, query: str, headers: dict) -> Request:
    scope = {
        "type": "http", "method": "POST", "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": query.encode(),
        "server": ("test", 80), "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.fixture(autouse=True)
def _reset_state():
    orchestrator._per_account._buckets = {}
    orchestrator._per_did._buckets = {}
    orchestrator.callctx._pending = {}
    outbound_identities._entries = {}
    accounts._accounts = {
        ("fake", "cfg-a"): Account(
            provider="fake", account_ref="cfg-a", tenant_id="t-1", tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=FakeProvider({}),
        ),
    }
    accounts._default_outbound = {"tenant-a": accounts._accounts[("fake", "cfg-a")]}
    accounts._loaded = True
    yield
    accounts._accounts = {}
    accounts._default_outbound = {}
    accounts._loaded = False
    outbound_identities._entries = {}


@pytest.mark.asyncio
async def test_place_call_remembers_identity_for_the_answer_callback():
    identity = OutboundIdentity(
        tenant_id=uuid.uuid4(), tenant_slug="tenant-a", agent_slug="sales", from_number="1555",
    )
    key = f"k-{uuid.uuid4()}"

    await outbound.place_call(provider="fake", identity=identity, to_number="9", idempotency_key=key)

    assert outbound_identities.recall("fake", "cfg-a", key) == ("tenant-a", "sales")


@pytest.mark.asyncio
async def test_answer_webhook_for_outbound_call_uses_remembered_agent_not_default(monkeypatch):
    key = f"k-{uuid.uuid4()}"
    outbound_identities.remember("fake", "cfg-a", key, tenant_slug="tenant-a", agent_slug="sales")

    async def _did_route_should_not_be_called(did: str):
        raise AssertionError("DID resolution must not run for a recognized outbound answer callback")

    monkeypatch.setattr(orchestrator.did_route, "resolve_did_route", _did_route_should_not_be_called)

    query = f"idem={key}&call_id=call-1&from=1000&to=2000"
    resp = await orchestrator.handle_inbound_webhook(
        provider_name="fake", account_ref="cfg-a",
        request=_make_request(path="/fake/voice/cfg-a", query=query, headers={"x-fake-signature": "valid"}),
    )
    assert resp.status_code == 200

    body = resp.body.decode()
    token = body.rsplit("/fake/stream/", 1)[1]
    route = orchestrator.callctx.claim(token)
    assert route is not None
    assert route.tenant_slug == "tenant-a"
    assert route.agent_slug == "sales"
    assert route.direction == "outbound"


@pytest.mark.asyncio
async def test_answer_webhook_for_outbound_call_to_a_foreign_tenants_did_is_not_rejected(monkeypatch):
    """The callee number happens to be provisioned as tenant-b's DID in the
    did: cache — this must not trip the foreign_did rejection for a call
    this service itself placed for tenant-a."""
    key = f"k-{uuid.uuid4()}"
    outbound_identities.remember("fake", "cfg-a", key, tenant_slug="tenant-a", agent_slug="sales")

    async def _resolve(did: str):
        return ("tenant-b", "some-agent")

    monkeypatch.setattr(orchestrator.did_route, "resolve_did_route", _resolve)

    query = f"idem={key}&call_id=call-1&from=1000&to=2000"
    resp = await orchestrator.handle_inbound_webhook(
        provider_name="fake", account_ref="cfg-a",
        request=_make_request(path="/fake/voice/cfg-a", query=query, headers={"x-fake-signature": "valid"}),
    )
    assert resp.status_code == 200
    assert b"/fake/stream/" in resp.body


@pytest.mark.asyncio
async def test_answer_webhook_without_a_recognized_idem_still_uses_did_resolution(monkeypatch):
    """A genuinely inbound call (no ?idem=, or one this process never
    remembered) must still take the ordinary DID-resolution path — the fix
    is additive, not a blanket bypass."""
    calls: list[str] = []

    async def _resolve(did: str):
        calls.append(did)
        return None

    monkeypatch.setattr(orchestrator.did_route, "resolve_did_route", _resolve)

    query = "call_id=call-1&from=1000&to=2000"
    resp = await orchestrator.handle_inbound_webhook(
        provider_name="fake", account_ref="cfg-a",
        request=_make_request(path="/fake/voice/cfg-a", query=query, headers={"x-fake-signature": "valid"}),
    )
    assert resp.status_code == 200
    assert calls == ["2000"]
