from __future__ import annotations

import pytest

from libs.tenancy import platform_conn
from services.campaigns import attempts
from services.campaigns.tests import seed


async def _create(pool, *, agent_max_call_s, path="sip"):
    async with pool.acquire() as conn:
        tenant_id = await seed.tenant(conn)
        camp = await seed.campaign(conn, tenant_id, agent_max_call_s=agent_max_call_s)
        cont = await seed.contact(conn, camp["id"])
    async with platform_conn(pool, reason="test") as conn:
        return await attempts.create_attempt(
            conn, campaign=camp, contact=cont, path=path, provider=None if path == "sip" else "fake",
            cps_scope="sip_trunk:pstn",
        )


@pytest.mark.parametrize("agent_limit, expected", [(900, 900), (None, 7200), (30, 30), (7200, 7200)])
async def test_max_call_s_follows_agent_limit(scratch_pool, agent_limit, expected):
    row = await _create(scratch_pool, agent_max_call_s=agent_limit)
    assert row["max_call_s"] == expected


@pytest.mark.parametrize("raw, expected", [(10, 30), (9999, 7200)])
async def test_max_call_s_is_clamped_into_the_check_range(scratch_pool, raw, expected):
    # The agents CHECK keeps real rows in range, so lift it to prove the clamp is what protects the attempts CHECK.
    await scratch_pool.execute("ALTER TABLE agents DROP CONSTRAINT agents_max_call_duration_s_check")
    try:
        row = await _create(scratch_pool, agent_max_call_s=raw)
    finally:
        await scratch_pool.execute("UPDATE agents SET max_call_duration_s = NULL WHERE max_call_duration_s NOT BETWEEN 30 AND 7200")
        await scratch_pool.execute(
            "ALTER TABLE agents ADD CONSTRAINT agents_max_call_duration_s_check "
            "CHECK (max_call_duration_s IS NULL OR max_call_duration_s BETWEEN 30 AND 7200)",
        )
    assert row["max_call_s"] == expected


async def test_sip_attempt_defaults(scratch_pool):
    row = await _create(scratch_pool, agent_max_call_s=None)
    assert row["state"] == "pending" and row["send_count"] == 1 and row["billing_state"] == "off"
    assert row["call_session_id"] is not None and row["last_send_at"] is not None
    assert row["callee"].startswith("+") and row["caller_id"] == "+14155550100"
    assert row["idempotency_key"] == attempts.idempotency_key(row["contact_id"], 1)
    lead = await scratch_pool.fetchval(
        "SELECT extract(epoch FROM deadline_at - created_at) FROM campaign_attempts WHERE id = $1", row["id"],
    )
    assert round(lead) == 120


async def test_cpaas_attempt_has_no_session_id_but_still_a_time_limit(scratch_pool):
    row = await _create(scratch_pool, agent_max_call_s=None, path="cpaas")
    assert row["call_session_id"] is None and row["max_call_s"] == 7200 and row["provider"] == "fake"


async def test_attempt_number_follows_existing_attempts(scratch_pool):
    async with scratch_pool.acquire() as conn:
        tenant_id = await seed.tenant(conn)
        camp = await seed.campaign(conn, tenant_id)
        cont = await seed.contact(conn, camp["id"])
        await seed.attempt(conn, camp, cont, n=1, state="resolved", outcome="busy")
    async with platform_conn(scratch_pool, reason="test") as conn:
        row = await attempts.create_attempt(
            conn, campaign=camp, contact=cont, path="sip", provider=None, cps_scope="sip_trunk:pstn",
        )
    assert row["attempt"] == 2 and row["idempotency_key"].endswith("_2")
