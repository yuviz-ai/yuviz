"""The status webhook publishes call outcomes to campaign:events:cpaas, behind signature and replay checks."""

from __future__ import annotations

import pathlib
import time
import uuid

import pytest
import redis as sync_redis
from fastapi.testclient import TestClient

from libs.telephony_sdk import campaign_events
from libs.telephony_sdk.providers.fake import FakeProvider
from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider, _expected_signature
from services.telephony import callctx
from services.telephony import app as app_module
from services.telephony.accounts import Account, accounts

TENANT = str(uuid.uuid4())
TOKEN = "vobiz-auth-token"
BASE = "http://testserver/vobiz/status/cfg-v"
STREAM = campaign_events.STREAM_CPAAS


class _CoveringFake(FakeProvider):
    signature_covers_fields = True

    def verify_webhook_signature(self, url, headers):
        return True


def _account(provider: str, instance) -> Account:
    return Account(
        provider=provider, account_ref="cfg-v", tenant_id=TENANT, tenant_slug="tenant-a",
        is_default_outbound=True, credentials={}, instance=instance,
    )


def _signed(nonce: str, bad: bool = False) -> dict[str, str]:
    signature = _expected_signature("wrong-token" if bad else TOKEN, BASE, nonce, "v3")
    return {"x-vobiz-signature-v3": signature, "x-vobiz-signature-v3-nonce": nonce}


@pytest.fixture
def stream(_scratch_redis_port, monkeypatch):
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{_scratch_redis_port}/0")
    client = sync_redis.Redis(port=_scratch_redis_port, decode_responses=True)
    client.flushall()
    yield client
    client.close()


@pytest.fixture
def http(stream, monkeypatch):
    async def _noop_refresh(self):
        return None

    monkeypatch.setattr(accounts.__class__, "refresh", _noop_refresh)
    accounts._accounts = {("vobiz", "cfg-v"): _account("vobiz", VobizTelephonyProvider({"auth_id": "A", "auth_token": TOKEN}))}
    accounts._loaded = True
    with TestClient(app_module.app) as c:
        yield c
    accounts._accounts = {}
    accounts._loaded = False


def _hangup(http, key: str, headers: dict[str, str]):
    return http.post(
        "/vobiz/status/cfg-v", params={"idem": key, "event": "hangup"}, data={"call_uuid": "vendor-1", "status": "completed"},
        headers=headers,
    )


def test_missing_or_bad_signature_is_rejected_and_nothing_is_published(http, stream):
    key = f"{uuid.uuid4()}_1"
    assert _hangup(http, key, {}).status_code == 403
    assert _hangup(http, key, _signed("n-1", bad=True)).status_code == 403
    assert stream.xlen(STREAM) == 0


def test_signed_hangup_publishes_one_unconfirmed_entry_for_the_signing_tenant(http, stream):
    key = f"{uuid.uuid4()}_1"
    assert _hangup(http, key, _signed("n-1")).status_code == 200
    ((_, fields),) = stream.xrange(STREAM)
    assert fields["source"] == "cpaas" and fields["kind"] == "hangup"
    assert fields["key"] == key and fields["tenant_id"] == TENANT and fields["provider_call_id"] == "vendor-1"
    assert fields["unconfirmed"] == "1"


def test_replay_of_a_valid_request_is_rejected_and_adds_no_entry(http, stream):
    key = f"{uuid.uuid4()}_1"
    assert _hangup(http, key, _signed("n-1")).status_code == 200
    assert _hangup(http, key, _signed("n-1")).status_code == 403
    assert stream.xlen(STREAM) == 1
    assert _hangup(http, key, _signed("n-2")).status_code == 200
    assert stream.xlen(STREAM) == 2


def test_ring_event_publishes_nothing(http, stream):
    resp = http.post(
        "/vobiz/status/cfg-v", params={"idem": "k", "event": "ring"}, data={"call_uuid": "vendor-1"}, headers=_signed("n-1"),
    )
    assert resp.status_code == 200
    assert stream.xlen(STREAM) == 0


def test_a_provider_whose_signature_covers_the_fields_is_not_marked_unconfirmed(stream, monkeypatch):
    async def _noop_refresh(self):
        return None

    monkeypatch.setattr(accounts.__class__, "refresh", _noop_refresh)
    accounts._accounts = {("fake", "cfg-v"): _account("fake", _CoveringFake({}))}
    accounts._loaded = True
    with TestClient(app_module.app) as c:
        c.post("/fake/status/cfg-v", params={"idem": "k", "event": "hangup"}, data={"call_uuid": "c1"})
    accounts._accounts = {}
    accounts._loaded = False
    ((_, fields),) = stream.xrange(STREAM)
    assert "unconfirmed" not in fields


def test_an_outbound_session_start_publishes_answered_with_the_session_id(http, stream, monkeypatch):
    key = f"{uuid.uuid4()}_1"

    class _Bridge:
        def __init__(self, **kwargs):
            self._on_session_start = kwargs["on_session_start"]

        async def run(self, websocket):
            self._on_session_start("sess-1")

    monkeypatch.setattr(app_module, "MediaStreamBridge", _Bridge)
    token = app_module.orchestrator.callctx.issue(callctx.CallRoute(
        tenant_slug="tenant-a", agent_slug="sales", caller_did="d", called_did="c", provider="vobiz", account_ref="cfg-v",
        provider_call_id="vendor-1", issued_at=time.monotonic(), direction="outbound", idempotency_key=key,
    ))
    with http.websocket_connect(f"/vobiz/stream/{token}"):
        pass
    for _ in range(40):
        if stream.xlen(STREAM):
            break
        time.sleep(0.05)
    ((_, fields),) = stream.xrange(STREAM)
    assert (fields["kind"], fields["key"], fields["tenant_id"], fields["call_session_id"]) == ("answered", key, TENANT, "sess-1")
    assert "unconfirmed" not in fields


def test_no_module_still_pushes_outcomes_over_http():
    removed = "_notify_campaigns" + "_best_effort"
    root = pathlib.Path(__file__).resolve().parents[3]
    sources = [p for top in ("services", "libs") for p in (root / top).rglob("*.py")]
    assert len(sources) > 100
    assert [p for p in sources if removed in p.read_text() and p != pathlib.Path(__file__)] == []


def test_naming_another_tenants_key_gets_the_response_an_unknown_key_gets(http, stream):
    foreign = _hangup(http, f"{uuid.uuid4()}_1", _signed("n-foreign"))
    unknown = _hangup(http, "no-such-key", _signed("n-unknown"))
    assert (foreign.status_code, foreign.content) == (unknown.status_code, unknown.content) == (200, b"ok")
