from __future__ import annotations

import pytest

from services.campaigns import campaigns

pytestmark = pytest.mark.integration


async def test_create_and_get_campaign(test_tenant, test_agent):
    row = await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="Renewal reminders",
        caller_id="+14155550100", max_concurrent_calls=2, pacing_seconds=10, max_attempts=2,
    )
    assert row["status"] == "draft"
    assert row["caller_id"] == "+14155550100"

    fetched = await campaigns.get_campaign(row["id"])
    assert fetched["name"] == "Renewal reminders"


async def test_list_campaigns_scoped_to_tenant(test_tenant, test_agent):
    await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="A",
        caller_id=None, max_concurrent_calls=1, pacing_seconds=5, max_attempts=1,
    )
    result = await campaigns.list_campaigns(test_tenant["id"])
    assert any(c["name"] == "A" for c in result)


async def test_update_campaign_changes_only_given_fields(test_tenant, test_agent):
    row = await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="Original",
        caller_id="+14155550100", max_concurrent_calls=1, pacing_seconds=5, max_attempts=1,
    )
    updated = await campaigns.update_campaign(row["id"], {"name": "Renamed", "caller_id": None})
    assert updated["name"] == "Renamed"
    assert updated["caller_id"] == "+14155550100"  # None in the update dict is dropped, not applied


async def test_set_status_transitions(test_tenant, test_agent):
    row = await campaigns.create_campaign(
        test_tenant["id"], agent_id=test_agent["id"], name="X",
        caller_id="+14155550100", max_concurrent_calls=1, pacing_seconds=5, max_attempts=1,
    )
    running = await campaigns.set_status(row["id"], "running")
    assert running["status"] == "running"

    paused = await campaigns.set_status(row["id"], "paused")
    assert paused["status"] == "paused"


async def test_update_unknown_campaign_raises_lookup_error():
    with pytest.raises(LookupError):
        await campaigns.update_campaign("00000000-0000-0000-0000-000000000000", {"name": "x"})


async def test_get_progress_counts_by_status(test_tenant, test_agent, pool):
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
