from __future__ import annotations

import uuid

import pytest

from libs.tenancy import platform_conn
from services.campaigns import capacity
from services.campaigns.tests import seed

TRUNK = "sip_trunk:pstn"


@pytest.fixture(autouse=True)
async def _clean_limits(scratch_pool):
    await scratch_pool.execute("DELETE FROM campaign_attempts")
    await scratch_pool.execute("DELETE FROM outbound_cps_limits")
    await scratch_pool.execute("UPDATE platform_outbound_limits SET max_outbound_concurrent_calls = 225")


async def _fill(pool, camp, n, *, state="sent", outcome=None, last_send_at_ago_s=None, cps_scope=TRUNK):
    async with pool.acquire() as conn:
        for i in range(n):
            cont = await seed.contact(conn, camp["id"], status="calling", attempt_count=1)
            await seed.attempt(
                conn, camp, cont, state=state, outcome=outcome, cps_scope=cps_scope,
                last_send_at_ago_s=last_send_at_ago_s,
            )


async def _bulk_fill(pool, camp, n):
    await pool.execute(
        """
        WITH c AS (
            INSERT INTO campaign_contacts (campaign_id, phone_number, status, attempt_count)
            SELECT $1, '+1415555' || lpad(g::text, 4, '0'), 'calling', 1 FROM generate_series(1, $2) g RETURNING id
        )
        INSERT INTO campaign_attempts (tenant_id, campaign_id, contact_id, attempt, idempotency_key, path, state, deadline_at)
        SELECT $3, $1, c.id, 1, c.id::text || '_1', 'sip', 'sent', now() + interval '2 minutes' FROM c
        """,
        camp["id"], n, camp["tenant_id"],
    )


async def _admit(pool, camp, scope=TRUNK):
    async with platform_conn(pool, reason="test") as conn:
        return await capacity.admit(conn, camp, scope)


async def _world(pool, *, tenant_cap=None, campaign_cap=5):
    async with pool.acquire() as conn:
        tenant_id = await seed.tenant(conn, outbound_cap=tenant_cap)
        return await seed.campaign(conn, tenant_id, max_concurrent_calls=campaign_cap)


async def test_deployed_platform_limits_are_the_seeded_defaults(scratch_pool):
    row = await scratch_pool.fetchrow("SELECT * FROM platform_outbound_limits")
    assert (row["total_call_capacity"], row["inbound_reserve"], row["max_outbound_concurrent_calls"]) == (300, 75, 225)


async def test_admits_when_nothing_is_reached(scratch_pool):
    camp = await _world(scratch_pool)
    assert await _admit(scratch_pool, camp) is None


async def test_each_reason_in_isolation(scratch_pool):
    camp = await _world(scratch_pool, campaign_cap=2)
    await _fill(scratch_pool, camp, 2)
    assert await _admit(scratch_pool, camp) == "campaign_cap"

    camp = await _world(scratch_pool, tenant_cap=3)
    other = await seed_campaign_in(scratch_pool, camp["tenant_id"])
    await _fill(scratch_pool, camp, 2)
    await _fill(scratch_pool, other, 1)
    assert await _admit(scratch_pool, camp) == "tenant_cap"

    camp = await _world(scratch_pool)
    other_tenant = await _world(scratch_pool, campaign_cap=1000)
    await _bulk_fill(scratch_pool, other_tenant, 225)
    assert await _admit(scratch_pool, camp) == "platform_cap"

    await scratch_pool.execute("DELETE FROM campaign_attempts")
    camp = await _world(scratch_pool)
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 1)", TRUNK)
    await _fill(scratch_pool, camp, 1, last_send_at_ago_s=0)
    assert await _admit(scratch_pool, camp) == "trunk_cps"

    scope = f"provider_account:{uuid.uuid4()}"
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 1)", scope)
    await _fill(scratch_pool, camp, 1, last_send_at_ago_s=0, cps_scope=scope)
    assert await _admit(scratch_pool, camp, scope) == "provider_cps"


async def seed_campaign_in(pool, tenant_id):
    async with pool.acquire() as conn:
        return await seed.campaign(conn, tenant_id)


async def test_first_applicable_reason_wins(scratch_pool):
    # campaign cap 1 and tenant cap 1 are both reached: the campaign cap is reported.
    camp = await _world(scratch_pool, tenant_cap=1, campaign_cap=1)
    await _fill(scratch_pool, camp, 1, last_send_at_ago_s=0)
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 1)", TRUNK)
    assert await _admit(scratch_pool, camp) == "campaign_cap"

    # drop the campaign cap out of the way: tenant cap now outranks the cps window.
    await scratch_pool.execute("UPDATE campaigns SET max_concurrent_calls = 5 WHERE id = $1", camp["id"])
    assert await _admit(scratch_pool, camp) == "tenant_cap"

    # drop the tenant cap too: the trunk window is all that is left.
    await scratch_pool.execute("UPDATE tenants SET max_outbound_concurrent_calls = NULL WHERE id = $1", camp["tenant_id"])
    assert await _admit(scratch_pool, camp) == "trunk_cps"

    # a full platform cap outranks the cps window.
    other = await _world(scratch_pool, campaign_cap=1000)
    await _bulk_fill(scratch_pool, other, 224)
    assert await _admit(scratch_pool, camp) == "platform_cap"


@pytest.mark.parametrize(
    "state, outcome, counts",
    [("pending", None, True), ("sent", None, True), ("resolved", "answered", True),
     ("resolved", "ended", False), ("resolved", "busy", False), ("resolved", "failed", False)],
)
async def test_open_attempt_predicate(scratch_pool, state, outcome, counts):
    camp = await _world(scratch_pool, campaign_cap=1)
    await _fill(scratch_pool, camp, 1, state=state, outcome=outcome)
    assert (await _admit(scratch_pool, camp) == "campaign_cap") is counts


async def test_lowering_a_limit_applies_to_the_next_call(scratch_pool):
    camp = await _world(scratch_pool, tenant_cap=2)
    await _fill(scratch_pool, camp, 1)
    assert await _admit(scratch_pool, camp) is None
    await scratch_pool.execute("UPDATE tenants SET max_outbound_concurrent_calls = 1 WHERE id = $1", camp["tenant_id"])
    assert await _admit(scratch_pool, camp) == "tenant_cap"


async def test_null_tenant_cap_defers_to_platform_cap(scratch_pool):
    camp = await _world(scratch_pool, tenant_cap=None, campaign_cap=1000)
    await _bulk_fill(scratch_pool, camp, 224)
    assert await _admit(scratch_pool, camp) is None
    await _bulk_fill(scratch_pool, camp, 1)
    assert await _admit(scratch_pool, camp) == "platform_cap"


async def test_cps_window_expires(scratch_pool):
    camp = await _world(scratch_pool)
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 1)", TRUNK)
    await _fill(scratch_pool, camp, 1, last_send_at_ago_s=1.5)
    assert await _admit(scratch_pool, camp) is None


async def test_platform_limit_write_validation(scratch_pool):
    async with platform_conn(scratch_pool, reason="test") as conn:
        with pytest.raises(capacity.LimitError, match="outbound cap exceeds capacity"):
            await capacity.set_platform_limits(
                conn, total_call_capacity=300, inbound_reserve=75, max_outbound_concurrent_calls=226,
            )
        row = await capacity.set_platform_limits(
            conn, total_call_capacity=300, inbound_reserve=75, max_outbound_concurrent_calls=100,
        )
    assert row["max_outbound_concurrent_calls"] == 100
    assert (await scratch_pool.fetchrow("SELECT * FROM platform_outbound_limits"))["max_outbound_concurrent_calls"] == 100


async def test_tenant_cap_and_cps_write_validation(scratch_pool):
    async with scratch_pool.acquire() as conn:
        tenant_id = await seed.tenant(conn)
    async with platform_conn(scratch_pool, reason="test") as conn:
        with pytest.raises(capacity.LimitError):
            await capacity.set_tenant_cap(conn, tenant_id, 0)
        assert await capacity.set_tenant_cap(conn, tenant_id, 4) is True
        assert await capacity.set_tenant_cap(conn, uuid.uuid4(), 4) is False
        with pytest.raises(capacity.LimitError):
            await capacity.set_cps_limit(conn, "provider_account:not-a-uuid", 5)
        with pytest.raises(capacity.LimitError):
            await capacity.set_cps_limit(conn, TRUNK, 0)
        await capacity.set_cps_limit(conn, TRUNK, 3)
        await capacity.set_cps_limit(conn, TRUNK, 4)
    assert await scratch_pool.fetchval("SELECT cps FROM outbound_cps_limits WHERE scope = $1", TRUNK) == 4
    assert await scratch_pool.fetchval("SELECT max_outbound_concurrent_calls FROM tenants WHERE id = $1", tenant_id) == 4
