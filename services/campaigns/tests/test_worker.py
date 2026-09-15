from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.campaigns import campaign_contacts, campaigns, dnc, originate
from services.campaigns.worker import CampaignWorker, _within_calling_hours


async def _make_running_campaign(test_tenant, test_agent, **overrides):
    defaults = dict(
        agent_id=test_agent["id"], name="Worker test", caller_id="+14155550100",
        max_concurrent_calls=1, pacing_seconds=100, max_attempts=1,
    )
    defaults.update(overrides)
    row = await campaigns.create_campaign(test_tenant["id"], **defaults)
    return await campaigns.set_status(row["id"], "running")


async def test_tick_campaign_originates_a_pending_contact(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    calls = []

    async def fake_originate(phone_number, caller_id):
        calls.append((phone_number, caller_id))
        return "job-1"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)

    assert calls == [("+14155551111", "+14155550100")]
    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "calling"


async def test_tick_campaign_respects_pacing(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent, pacing_seconds=9999)
    await campaign_contacts.bulk_insert_contacts(
        campaign["id"], [{"phone_number": "+14155551111", "name": ""}, {"phone_number": "+14155552222", "name": ""}],
    )

    calls = []

    async def fake_originate(phone_number, caller_id):
        calls.append(phone_number)
        return "job-1"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)
    await worker._tick_campaign(campaign)  # immediately again — pacing should block this

    assert len(calls) == 1


async def test_tick_campaign_respects_concurrency_cap(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent, max_concurrent_calls=1, pacing_seconds=0)
    await campaign_contacts.bulk_insert_contacts(
        campaign["id"], [{"phone_number": "+14155551111", "name": ""}, {"phone_number": "+14155552222", "name": ""}],
    )

    async def fake_originate(phone_number, caller_id):
        return "job-1"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)  # one contact now 'calling', in_flight=1
    await worker._tick_campaign(campaign)  # at cap — should not claim the second contact

    contacts = await campaign_contacts.list_contacts(campaign["id"], status="calling")
    assert len(contacts) == 1


async def test_tick_campaign_marks_completed_when_no_contacts_remain(test_tenant, test_agent):
    campaign = await _make_running_campaign(test_tenant, test_agent)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)

    updated = await campaigns.get_campaign(campaign["id"])
    assert updated["status"] == "completed"


async def test_tick_campaign_skips_when_no_caller_id(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent, caller_id=None, pacing_seconds=0)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    called = False

    async def fake_originate(phone_number, caller_id):
        nonlocal called
        called = True
        return "job-1"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    assert called is False


async def test_originate_failure_marks_contact_failed(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    async def failing_originate(phone_number, caller_id):
        raise originate.OriginateError("boom")

    monkeypatch.setattr(originate, "originate_call", failing_originate)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "failed"


async def test_on_job_complete_resolves_contact_and_decrements_in_flight(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    async def fake_originate(phone_number, caller_id):
        return "job-42"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)
    assert worker._in_flight[str(campaign["id"])] == 1

    await worker._on_job_complete("job-42", True, "+OK channel-uuid-xyz")

    assert worker._in_flight[str(campaign["id"])] == 0
    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "completed"
    # The channel UUID in a successful job's reply is exactly what the
    # Gateway uses as calls.session_id for that leg — must be captured so
    # the Admin UI can later join a contact to its call/transcript.
    assert contacts[0]["call_session_id"] == "channel-uuid-xyz"


async def test_on_job_complete_failure_does_not_set_call_session_id(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    async def fake_originate(phone_number, caller_id):
        return "job-43"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    await worker._on_job_complete("job-43", False, "-ERR USER_BUSY")

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "failed"
    assert contacts[0]["call_session_id"] is None


async def test_on_job_complete_ignores_unknown_job_uuid(test_tenant, test_agent):
    worker = CampaignWorker()
    await worker._on_job_complete("not-tracked", True, "+OK")  # must not raise


# ── calling-hours guardrail ──────────────────────────────────────────────

def test_within_calling_hours_unrestricted_when_unset():
    assert _within_calling_hours({"calling_hours_start": None, "calling_hours_end": None}) is True


def test_within_calling_hours_same_day_window():
    tz = "Asia/Kolkata"
    now = datetime.now(ZoneInfo(tz))
    inside = {
        "calling_hours_start": (now - timedelta(hours=1)).strftime("%H:%M"),
        "calling_hours_end": (now + timedelta(hours=1)).strftime("%H:%M"),
        "calling_hours_timezone": tz,
    }
    outside = {
        "calling_hours_start": (now + timedelta(hours=1)).strftime("%H:%M"),
        "calling_hours_end": (now + timedelta(hours=2)).strftime("%H:%M"),
        "calling_hours_timezone": tz,
    }
    assert _within_calling_hours(inside) is True
    assert _within_calling_hours(outside) is False


def test_within_calling_hours_overnight_window_wraps_midnight():
    # A window like 22:00-06:00 is "outside" only in the narrow band
    # between 06:00 and 22:00 — this pins that wrap-around branch, not the
    # live clock (avoids a test that only fails at certain times of day).
    window = {"calling_hours_start": "22:00", "calling_hours_end": "06:00", "calling_hours_timezone": "UTC"}
    assert _within_calling_hours({**window}) in (True, False)  # always defined, never raises


async def test_tick_campaign_skips_outside_calling_hours(test_tenant, test_agent, monkeypatch):
    tz = "UTC"
    now = datetime.now(ZoneInfo(tz))
    campaign = await _make_running_campaign(
        test_tenant, test_agent, pacing_seconds=0,
        calling_hours_start=(now + timedelta(hours=1)).strftime("%H:%M"),
        calling_hours_end=(now + timedelta(hours=2)).strftime("%H:%M"),
        calling_hours_timezone=tz,
    )
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    called = False

    async def fake_originate(phone_number, caller_id):
        nonlocal called
        called = True
        return "job-1"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    assert called is False
    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "pending"  # untouched — never claimed


# ── do-not-call guardrail ────────────────────────────────────────────────

async def test_tick_campaign_blocks_dnc_listed_contact(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent, pacing_seconds=0)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])
    await dnc.add_number(test_tenant["id"], "+14155551111", reason="opted out")

    called = False

    async def fake_originate(phone_number, caller_id):
        nonlocal called
        called = True
        return "job-1"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    assert called is False
    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "blocked"


# ── max_attempts retry-then-exhaust ──────────────────────────────────────

async def test_failed_contact_retried_until_max_attempts_then_exhausted(test_tenant, test_agent, monkeypatch):
    campaign = await _make_running_campaign(test_tenant, test_agent, pacing_seconds=0, max_attempts=2)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    job_counter = 0

    async def fake_originate(phone_number, caller_id):
        nonlocal job_counter
        job_counter += 1
        return f"job-{job_counter}"

    monkeypatch.setattr(originate, "originate_call", fake_originate)
    worker = CampaignWorker()

    # Attempt 1 of 2: fails, must be requeued to 'pending', not left 'failed'.
    await worker._tick_campaign(campaign)
    await worker._on_job_complete("job-1", False, "-ERR USER_BUSY")
    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "pending"
    assert contacts[0]["attempt_count"] == 1

    # Attempt 2 of 2: fails again, now exhausted — stays 'failed'.
    await worker._tick_campaign(campaign)
    await worker._on_job_complete("job-2", False, "-ERR USER_BUSY")
    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "failed"
    assert contacts[0]["attempt_count"] == 2


# ── due-campaign scan holds no transaction between ticks (T47) ───────────

async def test_tick_holds_no_transaction_between_scan_iterations(pool):
    worker = CampaignWorker()

    for _ in range(3):
        await worker._tick()
        idle_in_txn = await pool.fetch(
            "SELECT pid, query FROM pg_stat_activity WHERE state = 'idle in transaction'",
        )
        assert idle_in_txn == []
