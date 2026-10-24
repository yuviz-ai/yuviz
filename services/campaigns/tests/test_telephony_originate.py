"""T21/T23: retry/backoff reuses one idempotency key across attempts, and a
202 raises TelephonyOriginatePending carrying that key."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from libs.telephony_sdk.exceptions import TelephonyRateLimited
from services.campaigns import attempts, telephony_originate as mod


class _Response:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text or str(json_body)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _ScriptedClient:
    """Each POST call consumes one entry from `responses` — a raised
    exception is raised, a status code becomes a `_Response`."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _reset_token(monkeypatch):
    mod._jwt_token = "test-token"
    yield
    mod._jwt_token = None


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(seconds):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _instant)


def _wire(monkeypatch, client: _ScriptedClient) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=None: client)


@pytest.mark.asyncio
async def test_retry_reuses_same_key_across_attempts(monkeypatch):
    client = _ScriptedClient([
        httpx.ConnectError("down"),
        _Response(500, text="server error"),
        _Response(201, {"ok": True, "call_uuid": "call-123"}),
    ])
    _wire(monkeypatch, client)

    call_id = await mod.originate_call(
        provider="vobiz", phone_number="+15551234567", caller_id="+15559999999",
        tenant_slug="tenant-a", agent_slug="sales", idempotency_key="fixed-key",
    )
    assert call_id == "call-123"
    assert len(client.calls) == 3
    keys_sent = {c["json"]["idempotency_key"] for c in client.calls}
    assert keys_sent == {"fixed-key"}


@pytest.mark.asyncio
async def test_202_raises_pending_with_key(monkeypatch):
    client = _ScriptedClient([_Response(202, {"status": "pending"})])
    _wire(monkeypatch, client)

    with pytest.raises(mod.TelephonyOriginatePending) as exc_info:
        await mod.originate_call(
            provider="vobiz", phone_number="+1", caller_id="+1",
            tenant_slug="tenant-a", agent_slug="sales", idempotency_key="pending-key",
        )
    assert exc_info.value.idempotency_key == "pending-key"


@pytest.mark.asyncio
async def test_exhausted_retries_raises_originate_error(monkeypatch):
    client = _ScriptedClient([
        _Response(500, text="e1"), _Response(500, text="e2"),
        _Response(500, text="e3"), _Response(500, text="e4"),
    ])
    _wire(monkeypatch, client)

    with pytest.raises(mod.TelephonyOriginateError):
        await mod.originate_call(
            provider="vobiz", phone_number="+1", caller_id="+1",
            tenant_slug="tenant-a", agent_slug="sales", idempotency_key="k",
        )
    assert len(client.calls) == 4  # 1 initial + 3 retries


@pytest.mark.asyncio
async def test_poll_idempotency_pending_returns_none(monkeypatch):
    client = _ScriptedClient([_Response(200, {"status": "pending"})])
    _wire(monkeypatch, client)

    result = await mod.poll_idempotency(provider="vobiz", tenant_slug="tenant-a", idempotency_key="k")
    assert result is None


@pytest.mark.asyncio
async def test_poll_idempotency_done_returns_call_id(monkeypatch):
    client = _ScriptedClient([_Response(200, {"ok": True, "call_uuid": "call-999"})])
    _wire(monkeypatch, client)

    result = await mod.poll_idempotency(provider="vobiz", tenant_slug="tenant-a", idempotency_key="k")
    assert result == "call-999"


_TELEPHONY_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def test_dial_key_passes_the_telephony_validator():
    key = attempts.idempotency_key(uuid4(), 3)
    assert key.endswith("_3")
    assert _TELEPHONY_KEY_RE.match(key)


@pytest.mark.asyncio
async def test_429_raises_rate_limited_with_retry_after_without_retrying(monkeypatch):
    client = _ScriptedClient([_Response(429, {"error": "rate_limited", "retry_after": 7})])
    _wire(monkeypatch, client)

    with pytest.raises(TelephonyRateLimited) as exc_info:
        await mod.originate_call(
            provider="vobiz", phone_number="+15551234567", caller_id="+15559999999",
            tenant_slug="tenant-a", agent_slug="sales", idempotency_key="k",
        )
    assert exc_info.value.retry_after == 7
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_a_full_call_logs_no_callee_digits(monkeypatch, caplog):
    client = _ScriptedClient([_Response(201, {"ok": True, "call_uuid": "call-123"})])
    _wire(monkeypatch, client)

    with caplog.at_level(logging.DEBUG):
        await mod.originate_call(
            provider="vobiz", phone_number="+15551234567", caller_id="+15559999999",
            tenant_slug="tenant-a", agent_slug="sales", idempotency_key="k",
        )
    assert caplog.records
    assert "5551234567" not in caplog.text


@pytest.mark.asyncio
async def test_reconcile_call_posts_the_contract_body(monkeypatch):
    client = _ScriptedClient([_Response(200, {"outcome": "placed", "provider_call_id": "c1"})])
    _wire(monkeypatch, client)
    after = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)

    result = await mod.reconcile_call(
        provider="vobiz", tenant_slug="tenant-a", idempotency_key="k_1",
        to="+15551234567", caller_id="+15559999999", created_after=after,
    )
    assert result == {"outcome": "placed", "provider_call_id": "c1"}
    assert client.calls[0]["url"].endswith("/vobiz/call/reconcile")
    assert client.calls[0]["json"] == {
        "tenant_slug": "tenant-a", "idempotency_key": "k_1",
        "to": "+15551234567", "from": "+15559999999", "created_after": after.isoformat(),
    }


@pytest.mark.asyncio
async def test_call_status_and_readiness_hit_their_routes(monkeypatch):
    client = _ScriptedClient([
        _Response(200, {"status": "busy", "to": "+1", "from": "+2", "billable_ms": None}),
        _Response(200, {"ready": False, "reason": "provider_not_campaign_capable"}),
    ])
    _wire(monkeypatch, client)

    status = await mod.call_status(provider="vobiz", tenant_slug="tenant-a", call_id="c1")
    ready = await mod.outbound_readiness(provider="cloudonix", tenant_slug="tenant-a")
    assert status["status"] == "busy"
    assert ready["ready"] is False
    assert client.calls[0]["url"].endswith("/vobiz/call/c1/status")
    assert client.calls[0]["params"] == {"tenant_slug": "tenant-a"}
    assert client.calls[1]["url"].endswith("/cloudonix/outbound-readiness")


@pytest.mark.asyncio
async def test_status_error_raises_originate_error(monkeypatch):
    client = _ScriptedClient([_Response(500, text="boom"), httpx.ConnectError("down")])
    _wire(monkeypatch, client)

    with pytest.raises(mod.TelephonyOriginateError):
        await mod.call_status(provider="vobiz", tenant_slug="t", call_id="c1")
    with pytest.raises(mod.TelephonyOriginateError):
        await mod.call_status(provider="vobiz", tenant_slug="t", call_id="c1")
