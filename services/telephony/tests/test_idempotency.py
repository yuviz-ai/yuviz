from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import idempotency, outbound
from services.telephony.accounts import Account, accounts
from services.telephony.ownership import OutboundIdentity


def _identity(tenant_id) -> OutboundIdentity:
    return OutboundIdentity(tenant_id=tenant_id, tenant_slug="tenant-a", agent_slug="sales", from_number="1555")


def _wire_account(tenant_id, *, credentials: dict | None = None) -> FakeProvider:
    fake = FakeProvider(credentials or {})
    accounts._default_outbound["tenant-a"] = Account(
        provider="fake", account_ref="cfg-fake", tenant_id=str(tenant_id), tenant_slug="tenant-a",
        is_default_outbound=True, credentials={}, instance=fake,
    )
    return fake


@pytest.fixture(autouse=True)
def _reset_accounts():
    accounts._default_outbound = {}
    yield
    accounts._default_outbound = {}


@pytest.mark.asyncio
async def test_second_finalized_call_returns_cached_body_without_touching_provider():
    tenant_id = uuid.uuid4()
    fake = _wire_account(tenant_id)
    key = f"key-{uuid.uuid4()}"

    status1, body1 = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    assert status1 == 200
    assert body1["ok"] is True
    assert fake.dial_count == 1

    status2, body2 = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    assert status2 == 200
    assert body2 == body1
    assert fake.dial_count == 1  # no second dial


@pytest.mark.asyncio
async def test_never_finalized_key_returns_202_within_bounded_window():
    tenant_id = uuid.uuid4()
    _wire_account(tenant_id)
    key = f"key-{uuid.uuid4()}"

    won = await idempotency.claim("fake", tenant_id, key)
    assert won is True  # simulate a first request still in flight, never finalized

    start = time.monotonic()
    status, body = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    elapsed = time.monotonic() - start

    assert status == 202
    assert body["status"] == "pending"
    assert idempotency.POLL_BUDGET_S - 1.0 <= elapsed <= idempotency.POLL_BUDGET_S + 2.0


@pytest.mark.asyncio
async def test_timeout_reconcile_placed_returns_200_with_real_id():
    tenant_id = uuid.uuid4()
    fake = _wire_account(tenant_id, credentials={
        "initiate_should_timeout": True, "reconcile_outcome": "placed",
    })
    key = f"key-{uuid.uuid4()}"

    status, body = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    assert status == 200
    assert body["ok"] is True
    assert "call_uuid" in body


@pytest.mark.asyncio
async def test_timeout_reconcile_not_placed_returns_ok_false():
    tenant_id = uuid.uuid4()
    _wire_account(tenant_id, credentials={
        "initiate_should_timeout": True, "reconcile_outcome": "not_placed",
    })
    key = f"key-{uuid.uuid4()}"

    status, body = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    assert status == 200
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_timeout_reconcile_indeterminate_returns_202():
    tenant_id = uuid.uuid4()
    _wire_account(tenant_id, credentials={
        "initiate_should_timeout": True, "reconcile_outcome": "indeterminate",
    })
    key = f"key-{uuid.uuid4()}"

    status, body = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    assert status == 202
    assert body["status"] == "pending"
