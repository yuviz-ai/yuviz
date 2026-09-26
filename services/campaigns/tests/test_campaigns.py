from __future__ import annotations

import pytest

from services.campaigns import campaigns


async def test_create_and_get_campaign(test_tenant, test_agent, scoped):
    row = await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="Renewal reminders",
        caller_id="+14155550100", max_concurrent_calls=2, pacing_seconds=10, max_attempts=2,
    )
    assert row["status"] == "draft"
    assert row["caller_id"] == "+14155550100"

    fetched = await campaigns.get_campaign(row["id"])
    assert fetched["name"] == "Renewal reminders"


async def test_list_campaigns_scoped_to_tenant(test_tenant, test_agent, scoped):
    await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="A",
        caller_id=None, max_concurrent_calls=1, pacing_seconds=5, max_attempts=1,
    )
    result = await campaigns.list_campaigns(test_tenant["id"])
    assert any(c["name"] == "A" for c in result)


async def test_update_campaign_changes_only_given_fields(test_tenant, test_agent, scoped):
    row = await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="Original",
        caller_id="+14155550100", max_concurrent_calls=1, pacing_seconds=5, max_attempts=1,
    )
    updated = await campaigns.update_campaign(row["id"], {"name": "Renamed", "caller_id": None})
    assert updated["name"] == "Renamed"
    assert updated["caller_id"] == "+14155550100"  # None in the update dict is dropped, not applied


async def test_set_status_transitions(test_tenant, test_agent, scoped):
    row = await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="X",
        caller_id="+14155550100", max_concurrent_calls=1, pacing_seconds=5, max_attempts=1,
    )
    running = await campaigns.set_status(row["id"], "running")
    assert running["status"] == "running"

    paused = await campaigns.set_status(row["id"], "paused")
    assert paused["status"] == "paused"


async def test_update_unknown_campaign_raises_lookup_error(scoped):
    with pytest.raises(LookupError):
        await campaigns.update_campaign("00000000-0000-0000-0000-000000000000", {"name": "x"})


# ── caller_id tenant ownership (security finding: unowned caller_id must
# never reach a dial) ─────────────────────────────────────────────────────

async def test_caller_id_owned_by_tenant_true_for_provisioned_did(test_tenant, pool, scoped):
    await pool.execute(
        "INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)",
        "+14155551234", test_tenant["id"],
    )
    try:
        assert await campaigns.caller_id_owned_by_tenant(test_tenant["id"], "+14155551234") is True
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did = $1", "+14155551234")


async def test_caller_id_owned_by_tenant_false_for_another_tenants_did(test_tenant, pool, scoped):
    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ('Other', $1) RETURNING id",
        f"other-{test_tenant['id']}",
    )
    await pool.execute(
        "INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)",
        "+14155559999", other["id"],
    )
    try:
        assert await campaigns.caller_id_owned_by_tenant(test_tenant["id"], "+14155559999") is False
    finally:
        await pool.execute("DELETE FROM phone_numbers WHERE did = $1", "+14155559999")
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def test_caller_id_owned_by_tenant_false_for_unprovisioned_number(test_tenant, scoped):
    assert await campaigns.caller_id_owned_by_tenant(test_tenant["id"], "+19995550000") is False


async def test_caller_id_owned_by_tenant_true_when_none(test_tenant):
    # A campaign with no caller_id configured yet is valid at create/update
    # time — worker.py's own "no caller_id configured" guard is what stops
    # it from dialing, not this check.
    assert await campaigns.caller_id_owned_by_tenant(test_tenant["id"], None) is True


async def test_get_progress_counts_by_status(test_tenant, test_agent, pool, scoped):
    row = await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="Progress test",
        caller_id="+14155550100", max_concurrent_calls=1, pacing_seconds=5, max_attempts=1,
    )
    await pool.execute(
        "INSERT INTO campaign_contacts (campaign_id, phone_number, status) VALUES "
        "($1, '+14155551111', 'pending'), ($1, '+14155552222', 'completed'), ($1, '+14155553333', 'failed')",
        row["id"],
    )
    progress = await campaigns.get_progress(row["id"])
    assert progress["total"] == 3
    assert progress["pending"] == 1
    assert progress["completed"] == 1
    assert progress["failed"] == 1
