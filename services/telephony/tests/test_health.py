from __future__ import annotations

import pytest

from libs.telephony_sdk.providers.fake import FakeProvider
from services.config import cache as config_cache
from services.telephony import health
from services.telephony.accounts import Account, accounts


def _wire(results: list[bool]) -> None:
    fake = FakeProvider({})
    calls = iter(results)

    async def _check_health():
        return next(calls)

    fake.check_health = _check_health  # type: ignore[method-assign]
    accounts._accounts = {
        ("fake", "cfg-a"): Account(
            provider="fake", account_ref="cfg-a", tenant_id="t-1", tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=fake,
        ),
    }


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    accounts._accounts = {}
    await config_cache.get_client().delete("telephony:health:cfg-a")


@pytest.mark.asyncio
async def test_no_probe_yet_is_standby():
    accounts._accounts = {}
    result = await config_cache.get_json("telephony:health:cfg-a")
    assert result is None  # absence of the key IS standby


@pytest.mark.asyncio
async def test_single_ok_probe_is_healthy():
    _wire([True])
    await health.probe_all()
    result = await config_cache.get_json("telephony:health:cfg-a")
    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_ok_then_fail_is_degraded():
    _wire([True])
    await health.probe_all()
    _wire([False])
    await health.probe_all()
    result = await config_cache.get_json("telephony:health:cfg-a")
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_fail_then_fail_is_degraded():
    _wire([False])
    await health.probe_all()
    _wire([False])
    await health.probe_all()
    result = await config_cache.get_json("telephony:health:cfg-a")
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_ttl_exceeds_interval():
    _wire([True])
    await health.probe_all()
    ttl = await config_cache.get_client().ttl("telephony:health:cfg-a")
    assert ttl > 300  # default TELEPHONY_HEALTH_INTERVAL_S
