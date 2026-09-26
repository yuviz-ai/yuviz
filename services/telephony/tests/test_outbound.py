from __future__ import annotations

import inspect
import uuid

import pytest

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import outbound
from services.telephony.accounts import Account, accounts
from services.telephony.ownership import OutboundIdentity


def _identity(tenant_id) -> OutboundIdentity:
    return OutboundIdentity(tenant_id=tenant_id, tenant_slug="tenant-a", agent_slug="sales", from_number="1555")


def _wire(tenant_id, credentials: dict | None = None) -> FakeProvider:
    fake = FakeProvider(credentials or {})
    accounts._default_outbound["tenant-a"] = Account(
        provider="fake", account_ref="cfg-fake", tenant_id=str(tenant_id), tenant_slug="tenant-a",
        is_default_outbound=True, credentials={}, instance=fake,
    )
    return fake


@pytest.fixture(autouse=True)
def _reset():
    accounts._default_outbound = {}
    yield
    accounts._default_outbound = {}


def test_place_call_and_send_sms_only_accept_outbound_identity():
    place_call_params = set(inspect.signature(outbound.place_call).parameters)
    send_sms_params = set(inspect.signature(outbound.send_sms).parameters)
    assert place_call_params == {"provider", "identity", "to_number", "idempotency_key"}
    assert send_sms_params == {"identity", "to_number", "text", "idempotency_key"}
    assert "tenant_slug" not in place_call_params | send_sms_params
    assert "agent_slug" not in place_call_params | send_sms_params
    assert "from_number" not in place_call_params | send_sms_params


@pytest.mark.asyncio
async def test_outcome_claim_won_answered():
    tenant_id = uuid.uuid4()
    _wire(tenant_id)
    status, body = await outbound.place_call(
        provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=f"k-{uuid.uuid4()}",
    )
    assert status == 200
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_outcome_claim_won_answered_failure_path():
    tenant_id = uuid.uuid4()
    _wire(tenant_id, {"initiate_should_fail": True})
    status, body = await outbound.place_call(
        provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=f"k-{uuid.uuid4()}",
    )
    assert status == 200
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_send_sms_outcome():
    tenant_id = uuid.uuid4()
    _wire(tenant_id)
    status, body = await outbound.send_sms(
        identity=_identity(tenant_id), to_number="9", text="hi", idempotency_key=f"k-{uuid.uuid4()}",
    )
    assert status == 200
    assert body["ok"] is True
    assert "message_id" in body
