from __future__ import annotations

import uuid

import pytest

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import idempotency, outbound
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


async def test_rate_limited_call_releases_the_claim_and_a_retry_reaches_the_provider(scratch_redis):
    tenant_id = uuid.uuid4()
    fake = _wire(tenant_id, {"initiate_rate_limited_calls": 1})
    key = f"{uuid.uuid4()}_1"

    status, body = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    assert (status, body) == (429, {"error": "rate_limited", "retry_after": 7})
    assert await idempotency.read("fake", tenant_id, key) is None
    assert fake.dial_count == 1

    status, body = await outbound.place_call(provider="fake", identity=_identity(tenant_id), to_number="9", idempotency_key=key)
    assert status == 200 and body["ok"] is True
    assert fake.dial_count == 2


async def test_release_removes_only_that_tenants_claim(scratch_redis):
    mine, other = uuid.uuid4(), uuid.uuid4()
    assert await idempotency.claim("fake", mine, "k") and await idempotency.claim("fake", other, "k")
    await idempotency.release("fake", mine, "k")
    assert await idempotency.read("fake", mine, "k") is None
    assert await idempotency.read("fake", other, "k") is not None


@pytest.mark.parametrize("scripted, expected_outcome, expects_id", [
    ("placed", "placed", True), ("not_placed", "not_placed", False), ("indeterminate", "unknown", False),
])
async def test_reconcile_maps_provider_outcomes(scratch_redis, scripted, expected_outcome, expects_id):
    tenant_id = uuid.uuid4()
    _wire(tenant_id, {"reconcile_outcome": scripted})
    result = await outbound.reconcile(provider="fake", tenant_id=tenant_id, tenant_slug="tenant-a", idempotency_key="k_1")
    assert result["outcome"] == expected_outcome
    assert (result["provider_call_id"] is not None) is expects_id


async def test_reconcile_uses_the_call_id_the_status_webhook_noted(scratch_redis):
    tenant_id = uuid.uuid4()
    _wire(tenant_id, {"reconcile_outcome": "placed"})
    await idempotency.note_reference("fake", tenant_id, "k_1", "call-from-webhook")
    result = await outbound.reconcile(provider="fake", tenant_id=tenant_id, tenant_slug="tenant-a", idempotency_key="k_1")
    assert result == {"outcome": "placed", "provider_call_id": "call-from-webhook"}


@pytest.mark.parametrize("provider_state, expected", [
    ("queued", "live"), ("ringing", "live"), ("in-progress", "answered"), ("completed", "ended"), ("busy", "busy"),
    ("no-answer", "no_answer"), ("rejected", "declined"), ("canceled", "declined"), ("failed", "failed"),
    ("something-new", "unknown"), ("", "unknown"),
])
async def test_call_status_maps_provider_states(provider_state, expected):
    tenant_id = uuid.uuid4()
    _wire(tenant_id, {"call_statuses": {"c1": {"status": provider_state, "to": "919876543210", "from": "+918065354620"}}})
    result = await outbound.call_status(provider="fake", tenant_slug="tenant-a", call_id="c1")
    assert result == {"status": expected, "to": "+919876543210", "from": "+918065354620"}


async def test_call_status_for_a_call_the_account_does_not_know_is_unknown():
    _wire(uuid.uuid4(), {"call_statuses": {"c1": {"status": "completed"}}})
    result = await outbound.call_status(provider="fake", tenant_slug="tenant-a", call_id="someone-elses-call")
    assert result == {"status": "unknown", "to": None, "from": None}


async def test_reconcile_and_status_need_a_default_outbound_account():
    with pytest.raises(outbound.OutboundUnavailable):
        await outbound.call_status(provider="fake", tenant_slug="tenant-a", call_id="c1")
