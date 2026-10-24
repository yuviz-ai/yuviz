"""Executable form of the billing seam contract. Billing's client must pass it before replacing NullBillingAdmission."""

from __future__ import annotations

import asyncio
import inspect
import math
import pathlib
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from services.campaigns import billing
from services.campaigns.billing import (
    BillingAdmission, BillingDisabled, Granted, NullBillingAdmission, Refused, Settlement,
)

ROOT = pathlib.Path(__file__).resolve().parents[3]
_RATE_PER_S = 1


class LedgerFake:
    """Charges one minor unit per second; `charge_cap=False` is a broken implementation for the negative test."""

    enabled = True

    def __init__(self, *, charge_cap: bool = True, latency_s: float = 0.0, credit_s: int = 3600) -> None:
        self.charge_cap = charge_cap
        self.latency_s = latency_s
        self.credit_s = credit_s
        self.grants: dict[str, int] = {}
        self.settled: dict[str, Settlement] = {}
        self.released: set[str] = set()
        self.concurrency_limit_checks = 0

    async def account_status(self, tenant_id):
        return billing.AccountStatus(True, self.credit_s * _RATE_PER_S, "USD", 1800)

    async def estimate(self, tenant_id, *, path, provider, destination_country, seconds):
        return seconds * _RATE_PER_S

    async def reserve(self, key, *, tenant_id, path, provider, destination_country, max_seconds, max_amount_minor, expires_at):
        assert destination_country is None or re.fullmatch(r"[A-Z]{2}", destination_country)
        try:
            return await asyncio.wait_for(self._reserve(key, max_seconds), billing.BILLING_TIMEOUT_S)
        except asyncio.TimeoutError:
            return Refused("unavailable")

    async def _reserve(self, key, max_seconds):
        await asyncio.sleep(self.latency_s)
        if key in self.grants:
            return Granted(self.grants[key], self.grants[key] * _RATE_PER_S)
        if self.credit_s <= 0:
            return Refused("insufficient_credit")
        granted = min(max_seconds, self.credit_s)
        self.grants[key] = granted
        return Granted(granted, granted * _RATE_PER_S)

    async def authorize(self, key, *, expires_at):
        return None

    async def settle(self, key, *, billable_ms):
        if key in self.settled:
            return self.settled[key]
        if key not in self.grants:
            raise KeyError(key)
        seconds = billable_ms / 1000 if not self.charge_cap else min(billable_ms, self.grants[key] * 1000) / 1000
        self.settled[key] = Settlement(math.ceil(seconds * _RATE_PER_S), "USD")
        return self.settled[key]

    async def release(self, key):
        if key in self.grants:
            self.released.add(key)

    async def aclose(self):
        return None


def _reserve_args(max_seconds=60):
    return dict(
        tenant_id=uuid.uuid4(), path="sip", provider=None, destination_country="IN", max_seconds=max_seconds,
        max_amount_minor=None, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


async def check_contract(impl: BillingAdmission) -> None:
    key = billing.billing_key(uuid.uuid4())

    # 1. reserve is idempotent per key; a Refused leaves no record.
    first = await impl.reserve(key, **_reserve_args())
    assert isinstance(first, Granted)
    assert await impl.reserve(key, **_reserve_args(max_seconds=600)) == first

    # 2. a campaign: key is never refused for outbound concurrency.
    for _ in range(25):
        refused = await impl.reserve(billing.billing_key(uuid.uuid4()), **_reserve_args())
        assert refused != Refused("concurrency")

    # 3. authorize and release on unknown keys are no-ops; settle charges at most once, also after release.
    unknown = billing.billing_key(uuid.uuid4())
    await impl.authorize(unknown, expires_at=datetime.now(timezone.utc))
    await impl.release(unknown)
    await impl.release(key)
    settled = await impl.settle(key, billable_ms=30_000)
    assert await impl.settle(key, billable_ms=50_000) == settled

    # 4. no phone number crosses the seam; the destination is an ISO-2 region.
    for name in ("reserve", "estimate"):
        params = inspect.signature(getattr(BillingAdmission, name)).parameters
        assert not {"phone", "phone_number", "callee", "number", "to"} & set(params)
        assert "destination_country" in params

    # 5. a reserve that outlasts the bound is Refused("unavailable"), not a hang.
    started = time.monotonic()
    slow = await _slow_twin(impl).reserve(billing.billing_key(uuid.uuid4()), **_reserve_args())
    assert slow == Refused("unavailable") and time.monotonic() - started < billing.BILLING_TIMEOUT_S + 1

    # 6. settle never charges more than the grant, however large the reported duration.
    capped_key, full_key = (billing.billing_key(uuid.uuid4()) for _ in range(2))
    granted = await impl.reserve(capped_key, **_reserve_args(max_seconds=60))
    await impl.reserve(full_key, **_reserve_args(max_seconds=60))
    at_grant = await impl.settle(full_key, billable_ms=granted.max_seconds * 1000)
    assert (await impl.settle(capped_key, billable_ms=999_999_999)).amount_minor <= at_grant.amount_minor
    with pytest.raises(Exception):
        await impl.settle(billing.billing_key(uuid.uuid4()), billable_ms=1000)  # no recorded grant


def _slow_twin(impl: LedgerFake) -> LedgerFake:
    return LedgerFake(charge_cap=impl.charge_cap, latency_s=billing.BILLING_TIMEOUT_S + 3)


async def test_ledger_fake_satisfies_the_contract():
    await check_contract(LedgerFake())


async def test_fake_that_charges_more_than_the_grant_fails_rule_6():
    with pytest.raises(AssertionError):
        await check_contract(LedgerFake(charge_cap=False))


def test_billing_key_format():
    attempt_id = uuid.uuid4()
    assert billing.billing_key(attempt_id) == f"campaign:{attempt_id}"


async def test_null_billing_raises_everywhere_but_account_status_and_aclose():
    null = NullBillingAdmission()
    assert null.enabled is False
    assert (await null.account_status(uuid.uuid4())).has_account is False
    assert await null.aclose() is None
    methods = {n for n, _ in inspect.getmembers(BillingAdmission, inspect.isfunction) if not n.startswith("_")}
    assert {"reserve", "settle", "release", "authorize", "estimate"} <= methods
    for name in methods - {"account_status", "aclose"}:
        with pytest.raises(BillingDisabled):
            await getattr(null, name)()


async def test_estimate_is_none_when_billing_is_disabled():
    assert await billing.estimate_campaign(None, NullBillingAdmission(), {}) is None


def test_nothing_on_the_call_path_references_billing():
    pattern = re.compile(r"campaigns[./]billing|BillingAdmission")
    roots = [ROOT / "services" / "conversation", ROOT / "services" / "gateway", ROOT / "gateway", ROOT / "scripts" / "freeswitch"]
    suffixes = {".py", ".lua", ".xml", ".sh", ".cpp", ".cc", ".h", ".hpp"}
    scanned = [
        p for root in roots if root.exists() for p in root.rglob("*")
        if p.is_file() and p.suffix in suffixes and "build" not in p.parts
    ]
    assert len(scanned) > 0
    assert [str(p) for p in scanned if pattern.search(p.read_text(errors="ignore"))] == []
