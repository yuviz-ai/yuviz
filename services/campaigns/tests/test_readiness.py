from __future__ import annotations

import asyncio
import logging
import typing
import uuid

import pytest

from services.campaigns import readiness, telephony_originate
from services.campaigns.billing import CostEstimate, NullBillingAdmission
from services.campaigns.tests import seed


class _Probe:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    async def probe(self) -> bool:
        return self.healthy


@pytest.fixture(autouse=True)
def _provider_ready(monkeypatch):
    async def ready(*, provider, tenant_slug):
        return {"ready": True, "reason": None}

    monkeypatch.setattr(telephony_originate, "outbound_readiness", ready)


async def _good(pool, *, provider: str | None = None, tenant_cap: int | None = None, **campaign_kwargs):
    """A campaign that passes every check; each test then breaks one thing."""
    async with pool.acquire() as conn:
        tenant_id = await seed.tenant(conn, outbound_cap=tenant_cap)
        camp = await seed.campaign(conn, tenant_id, **campaign_kwargs)
        await seed.contact(conn, camp["id"])
        config_id = None
        if provider is not None:
            config_id = await conn.fetchval(
                "INSERT INTO telephony_configs (tenant_id, provider, name) VALUES ($1, $2, 'cfg') RETURNING id",
                tenant_id, provider,
            )
        did = f"1{uuid.uuid4().int % 10**10:010d}"
        await conn.execute(
            "INSERT INTO phone_numbers (did, tenant_id, agent_id, telephony_config_id) VALUES ($1, $2, $3, $4)",
            did, tenant_id, camp["agent_id"], config_id,
        )
        await conn.execute("UPDATE campaigns SET caller_id = $2 WHERE id = $1", camp["id"], f"+{did}")
    return camp


async def _check(pool, camp, *, probe: bool = True):
    fresh = dict(await pool.fetchrow("SELECT * FROM campaigns WHERE id = $1", camp["id"]))
    return await readiness.check(fresh, tenant_id=fresh["tenant_id"], dial_client=_Probe(probe), billing=NullBillingAdmission())


def _ids(result, level="block"):
    return {c.id for c in result.checks if c.level == level}


async def test_a_fully_configured_sip_campaign_is_ready(scratch_pool):
    result = await _check(scratch_pool, await _good(scratch_pool))
    assert result.ready is True and result.checks == [] and result.estimate is None


async def test_a_fully_configured_provider_campaign_is_ready(scratch_pool):
    result = await _check(scratch_pool, await _good(scratch_pool, provider="fake"))
    assert result.ready is True and result.checks == []


async def _break_agent_missing(pool, camp):
    await pool.execute("UPDATE agents SET deleted_at = now() WHERE id = $1", camp["agent_id"])


async def _break_agent_inactive(pool, camp):
    await pool.execute("UPDATE agents SET status = 'inactive' WHERE id = $1", camp["agent_id"])


async def _break_agent_inbound_only(pool, camp):
    await pool.execute("UPDATE agents SET direction = 'inbound' WHERE id = $1", camp["agent_id"])


async def _break_agent_foreign(pool, camp):
    async with pool.acquire() as conn:
        other = await seed.campaign(conn, await seed.tenant(conn))
    await pool.execute("UPDATE campaigns SET agent_id = $2 WHERE id = $1", camp["id"], other["agent_id"])


_AGENT_BREAKS = [_break_agent_missing, _break_agent_inactive, _break_agent_inbound_only, _break_agent_foreign]


async def test_every_agent_failure_gives_the_same_message(scratch_pool):
    messages = set()
    for breaker in _AGENT_BREAKS:
        camp = await _good(scratch_pool)
        await breaker(scratch_pool, camp)
        result = await _check(scratch_pool, camp)
        [agent_check] = [c for c in result.checks if c.id == "agent_unavailable"]
        messages.add((agent_check.message, agent_check.field, agent_check.level))
    assert len(messages) == 1


async def _caller_id_missing(pool, camp):
    await pool.execute("UPDATE campaigns SET caller_id = NULL WHERE id = $1", camp["id"])


async def _caller_id_not_owned(pool, camp):
    await pool.execute("UPDATE campaigns SET caller_id = '+14155550999' WHERE id = $1", camp["id"])


async def _no_contacts(pool, camp):
    await pool.execute("UPDATE campaign_contacts SET status = 'completed' WHERE campaign_id = $1", camp["id"])


async def _bad_timezone(pool, camp):
    await pool.execute("UPDATE campaigns SET calling_hours_timezone = 'Mars/Base' WHERE id = $1", camp["id"])


_CASES = {
    "agent_unavailable": (_break_agent_missing, {}, True),
    "caller_id_missing": (_caller_id_missing, {}, True),
    "caller_id_not_owned": (_caller_id_not_owned, {}, True),
    "no_pending_contacts": (_no_contacts, {}, True),
    "timezone_invalid": (_bad_timezone, {}, True),
}


@pytest.mark.parametrize("expected_id", list(_CASES))
async def test_each_core_block_has_its_own_fixture(scratch_pool, expected_id):
    breaker, kwargs, probe = _CASES[expected_id]
    camp = await _good(scratch_pool, **kwargs)
    await breaker(scratch_pool, camp)
    result = await _check(scratch_pool, camp, probe=probe)
    assert expected_id in _ids(result) and result.ready is False


async def test_a_missing_caller_id_skips_the_route_dependent_checks(scratch_pool):
    camp = await _good(scratch_pool)
    await _caller_id_missing(scratch_pool, camp)
    assert _ids(await _check(scratch_pool, camp, probe=False)) == {"caller_id_missing"}


async def test_an_unowned_caller_id_does_not_probe_or_report_a_path(scratch_pool):
    camp = await _good(scratch_pool)
    await _caller_id_not_owned(scratch_pool, camp)
    assert _ids(await _check(scratch_pool, camp, probe=False)) == {"caller_id_not_owned"}


async def test_no_healthy_sip_node_blocks_a_native_did(scratch_pool):
    camp = await _good(scratch_pool)
    assert _ids(await _check(scratch_pool, camp, probe=False)) == {"no_healthy_sip_node"}


async def test_provider_not_ready_from_the_telephony_service(scratch_pool, monkeypatch):
    camp = await _good(scratch_pool, provider="fake")

    async def not_ready(*, provider, tenant_slug):
        return {"ready": False, "reason": "provider_unhealthy"}

    monkeypatch.setattr(telephony_originate, "outbound_readiness", not_ready)
    assert _ids(await _check(scratch_pool, camp)) == {"provider_not_ready"}

    async def down(*, provider, tenant_slug):
        raise telephony_originate.TelephonyOriginateError("down")

    monkeypatch.setattr(telephony_originate, "outbound_readiness", down)
    assert _ids(await _check(scratch_pool, camp)) == {"provider_not_ready"}


@pytest.mark.parametrize("provider", ["cloudonix", "not-a-registered-provider"])
async def test_provider_not_campaign_capable(scratch_pool, provider):
    camp = await _good(scratch_pool, provider=provider)
    assert _ids(await _check(scratch_pool, camp)) == {"provider_not_campaign_capable"}


async def test_provider_campaign_never_probes_the_sip_node(scratch_pool):
    camp = await _good(scratch_pool, provider="fake")
    assert _ids(await _check(scratch_pool, camp, probe=False)) == set()


async def test_campaign_cap_above_tenant_cap_warns_with_both_numbers_and_does_not_block(scratch_pool):
    camp = await _good(scratch_pool, tenant_cap=3, max_concurrent_calls=5)
    result = await _check(scratch_pool, camp)
    [warn] = [c for c in result.checks if c.id == "campaign_cap_above_tenant_cap"]
    assert warn.level == "warn" and "5" in warn.message and "3" in warn.message
    assert result.ready is True


async def test_capacity_never_blocks(scratch_pool):
    camp = await _good(scratch_pool, tenant_cap=1, max_concurrent_calls=1)
    async with scratch_pool.acquire() as conn:
        cont = await seed.contact(conn, camp["id"], status="calling", attempt_count=1)
        await seed.attempt(conn, camp, cont, state="sent")
    await scratch_pool.execute("UPDATE platform_outbound_limits SET max_outbound_concurrent_calls = 1")
    try:
        result = await _check(scratch_pool, camp)
    finally:
        await scratch_pool.execute("UPDATE platform_outbound_limits SET max_outbound_concurrent_calls = 225")
    assert result.ready is True and result.checks == []


async def test_with_null_billing_there_is_no_estimate_and_no_billing_check(scratch_pool):
    camp = await _good(scratch_pool)
    await _caller_id_missing(scratch_pool, camp)
    result = await _check(scratch_pool, camp)
    assert result.estimate is None
    assert not any(word in c.id for c in result.checks for word in ("billing", "budget", "credit", "balance"))


async def test_every_check_id_is_produced_by_some_fixture(scratch_pool, monkeypatch):
    produced: set[str] = set()
    for expected_id, (breaker, _, _) in _CASES.items():
        camp = await _good(scratch_pool)
        await breaker(scratch_pool, camp)
        produced |= {c.id for c in (await _check(scratch_pool, camp, probe=False)).checks}
    produced |= _ids(await _check(scratch_pool, await _good(scratch_pool, provider="cloudonix")))
    produced |= _ids(await _check(scratch_pool, await _good(scratch_pool, tenant_cap=1, max_concurrent_calls=2)), "warn")

    async def not_ready(*, provider, tenant_slug):
        return {"ready": False, "reason": "x"}

    monkeypatch.setattr(telephony_originate, "outbound_readiness", not_ready)
    produced |= _ids(await _check(scratch_pool, await _good(scratch_pool, provider="fake")))
    assert produced == set(typing.get_args(readiness.CheckId))


_ESTIMATE = CostEstimate(
    currency="USD", calls=1, avg_billable_s=60, avg_source="default", per_call_minor=12, total_minor=12,
)


async def test_a_stubbed_estimate_is_returned_with_readiness(scratch_pool, monkeypatch):
    async def stub(conn, billing, campaign):
        return _ESTIMATE

    monkeypatch.setattr(readiness, "estimate_campaign", stub)
    result = await _check(scratch_pool, await _good(scratch_pool))
    assert result.estimate == _ESTIMATE and result.ready is True


async def _slow(conn, billing, campaign):
    await asyncio.sleep(60)


async def _raises(conn, billing, campaign):
    raise NotImplementedError("billing estimate")


@pytest.mark.parametrize("stub", [_raises, _slow])
async def test_a_failing_or_slow_estimate_gives_none_and_frees_its_connection(scratch_pool, monkeypatch, stub, caplog):
    camp = await _good(scratch_pool)
    baseline = await _check(scratch_pool, camp)
    idle = scratch_pool.get_idle_size()
    monkeypatch.setattr(readiness, "estimate_campaign", stub)
    with caplog.at_level(logging.WARNING, logger=readiness.__name__):
        result = await _check(scratch_pool, camp)
    assert result.estimate is None and result.ready == baseline.ready and result.checks == baseline.checks
    assert scratch_pool.get_idle_size() == idle
    assert "cost estimate unavailable" in caplog.text
