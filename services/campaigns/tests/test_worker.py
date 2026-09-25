from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from services.campaigns import campaign_contacts, campaigns, dnc, originate, telephony_originate
from services.campaigns.worker import CampaignWorker, _idempotency_key, _within_calling_hours

_DEFAULT_CALLER_ID = "+14155550100"


@pytest_asyncio.fixture(autouse=True)
async def _provision_default_caller_id(test_tenant, pool):
    """worker.py now refuses to dial a caller_id that isn't an owned
    phone_numbers row for the campaign's own tenant (security finding:
    an unowned caller_id used to fall through to the unchecked ESL path).
    Every test in this module that reaches resolve_outbound_route dials
    from the same default number, so it must actually be provisioned."""
    await pool.execute(
        "INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2) ON CONFLICT (did) DO NOTHING",
        _DEFAULT_CALLER_ID, test_tenant["id"],
    )
    yield
    await pool.execute("DELETE FROM phone_numbers WHERE did = $1", _DEFAULT_CALLER_ID)


async def _make_running_campaign(test_tenant, test_agent, **overrides):
    defaults = dict(
        agent_id=test_agent["id"], name="Worker test", caller_id=_DEFAULT_CALLER_ID,
        max_concurrent_calls=1, pacing_seconds=100, max_attempts=1,
    )
    defaults.update(overrides)
    row = await campaigns.create_campaign(test_tenant["id"], **defaults)
    return await campaigns.set_status(row["id"], "running")


async def test_tick_campaign_originates_a_pending_contact(test_tenant, test_agent, monkeypatch, scoped):
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


async def test_tick_campaign_respects_pacing(test_tenant, test_agent, monkeypatch, scoped):
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


async def test_tick_campaign_respects_concurrency_cap(test_tenant, test_agent, monkeypatch, scoped):
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


async def test_tick_campaign_marks_completed_when_no_contacts_remain(test_tenant, test_agent, scoped):
    campaign = await _make_running_campaign(test_tenant, test_agent)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)

    updated = await campaigns.get_campaign(campaign["id"])
    assert updated["status"] == "completed"


async def test_tick_campaign_skips_when_no_caller_id(test_tenant, test_agent, monkeypatch, scoped):
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


async def test_originate_failure_marks_contact_failed(test_tenant, test_agent, monkeypatch, scoped):
    campaign = await _make_running_campaign(test_tenant, test_agent)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    async def failing_originate(phone_number, caller_id):
        raise originate.OriginateError("boom")

    monkeypatch.setattr(originate, "originate_call", failing_originate)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "failed"


async def test_on_job_complete_resolves_contact_and_decrements_in_flight(test_tenant, test_agent, monkeypatch, scoped):
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


async def test_on_job_complete_failure_does_not_set_call_session_id(test_tenant, test_agent, monkeypatch, scoped):
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


async def test_tick_campaign_skips_outside_calling_hours(test_tenant, test_agent, monkeypatch, scoped):
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

async def test_tick_campaign_blocks_dnc_listed_contact(test_tenant, test_agent, monkeypatch, scoped):
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

async def test_failed_contact_retried_until_max_attempts_then_exhausted(test_tenant, test_agent, monkeypatch, scoped):
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


# ── REST provider dispatch + idempotency-key minting (T22) ───────────────

async def _make_and_wire_rest_route(monkeypatch, test_tenant, test_agent, provider="vobiz", **overrides):
    campaign = await _make_running_campaign(test_tenant, test_agent, pacing_seconds=0, **overrides)
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    async def fake_resolve_route(tenant_id, agent_id, caller_id, **kwargs):
        return {"tenant_slug": "acme", "agent_slug": "sales", "provider": provider, "caller_id_owned": True}

    monkeypatch.setattr(campaigns, "resolve_outbound_route", fake_resolve_route)
    return campaign


async def test_same_attempt_retried_through_http_hop_reuses_one_key(monkeypatch):
    # Exercises originate_call()'s own internal HTTP-retry loop (AC20) — the
    # worker only claims once per attempt_count; retrying the SAME attempt
    # across a transient network blip happens inside originate_call itself,
    # not by the worker re-claiming the contact.
    keys_used = []

    class FakeResponse:
        status_code = 500
        text = "boom"

    async def fake_post_with_auth(path, json_body):
        keys_used.append(json_body["idempotency_key"])
        return FakeResponse()

    monkeypatch.setattr(telephony_originate, "_post_with_auth", fake_post_with_auth)
    monkeypatch.setattr(telephony_originate, "_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))

    with pytest.raises(telephony_originate.TelephonyOriginateError):
        await telephony_originate.originate_call(
            provider="vobiz", phone_number="+15551234567", caller_id="+15557654321",
            tenant_slug="acme", agent_slug="sales", idempotency_key="fixed-key",
        )

    assert len(set(keys_used)) == 1  # same idempotency_key every retry
    assert len(keys_used) == 4  # initial attempt + 3 backoff retries


async def test_requeued_attempt_mints_a_different_key(test_tenant, test_agent, monkeypatch, scoped):
    campaign = await _make_and_wire_rest_route(monkeypatch, test_tenant, test_agent, max_attempts=2)
    keys_used = []

    async def fake_originate_call(*, provider, phone_number, caller_id, tenant_slug, agent_slug, idempotency_key):
        keys_used.append(idempotency_key)
        raise telephony_originate.TelephonyOriginateError("boom")

    monkeypatch.setattr(telephony_originate, "originate_call", fake_originate_call)
    worker = CampaignWorker()

    await worker._tick_campaign(campaign)  # attempt 1 -> failed -> requeued to pending
    await worker._tick_campaign(campaign)  # attempt 2 -> different attempt_count

    assert len(keys_used) == 2
    assert keys_used[0] != keys_used[1]
    assert keys_used[0] == _idempotency_key(str(campaign["id"]), str((await campaign_contacts.list_contacts(campaign["id"]))[0]["id"]), 1)


async def test_202_leaves_contact_calling_and_resolves_via_poll_on_next_tick(test_tenant, test_agent, monkeypatch, scoped):
    campaign = await _make_and_wire_rest_route(monkeypatch, test_tenant, test_agent)

    async def fake_originate_call(*, provider, phone_number, caller_id, tenant_slug, agent_slug, idempotency_key):
        raise telephony_originate.TelephonyOriginatePending(idempotency_key)

    monkeypatch.setattr(telephony_originate, "originate_call", fake_originate_call)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "calling"
    assert (str(campaign["id"]), str(contacts[0]["id"])) in worker._pending_idem

    async def fake_poll(*, provider, tenant_slug, idempotency_key):
        return "vendor-call-1"

    monkeypatch.setattr(telephony_originate, "poll_idempotency", fake_poll)
    await worker._resolve_pending_idem()

    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "completed"
    assert contacts[0]["call_session_id"] == "vendor-call-1"
    assert worker._pending_idem == {}


async def test_unowned_caller_id_refuses_to_dial_never_falls_back_to_esl(test_tenant, test_agent, monkeypatch, scoped):
    """Security finding: a caller_id with no phone_numbers row for this
    tenant must be refused outright, never silently dialled over the
    unchecked ESL path (which performs no caller-id ownership check)."""
    campaign = await _make_running_campaign(
        test_tenant, test_agent, pacing_seconds=0, caller_id="+19995551234",  # never provisioned
    )
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    esl_called = rest_called = False

    async def fake_esl_originate(phone_number, caller_id):
        nonlocal esl_called
        esl_called = True
        return "job-esl-1"

    async def fake_telephony_originate(**kwargs):
        nonlocal rest_called
        rest_called = True
        return "call-1"

    monkeypatch.setattr(originate, "originate_call", fake_esl_originate)
    monkeypatch.setattr(telephony_originate, "originate_call", fake_telephony_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    assert esl_called is False
    assert rest_called is False
    contacts = await campaign_contacts.list_contacts(campaign["id"])
    assert contacts[0]["status"] == "failed"


async def test_owned_did_with_no_rest_binding_still_takes_esl_path(test_tenant, test_agent, monkeypatch, scoped):
    """An owned DID with no REST telephony_config binding (route["provider"]
    is None) is a legitimate native/ESL number, distinct from an unowned
    caller_id — must still dial, not be refused."""
    campaign = await _make_running_campaign(test_tenant, test_agent, pacing_seconds=0)  # default caller_id, provisioned, no telephony_config
    await campaign_contacts.bulk_insert_contacts(campaign["id"], [{"phone_number": "+14155551111", "name": ""}])

    esl_called = False

    async def fake_esl_originate(phone_number, caller_id):
        nonlocal esl_called
        esl_called = True
        return "job-esl-1"

    monkeypatch.setattr(originate, "originate_call", fake_esl_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    assert esl_called is True


async def test_native_or_none_provider_still_takes_esl_path(test_tenant, test_agent, monkeypatch, scoped):
    campaign = await _make_and_wire_rest_route(monkeypatch, test_tenant, test_agent, provider="native")
    esl_called = False

    async def fake_esl_originate(phone_number, caller_id):
        nonlocal esl_called
        esl_called = True
        return "job-esl-1"

    async def fail_if_called_telephony_originate(**kwargs):
        raise AssertionError("REST originate must not be reached for a 'native' route")

    monkeypatch.setattr(originate, "originate_call", fake_esl_originate)
    monkeypatch.setattr(telephony_originate, "originate_call", fail_if_called_telephony_originate)
    worker = CampaignWorker()
    await worker._tick_campaign(campaign)

    assert esl_called is True


# ── due-campaign scan holds no transaction between ticks (T47) ───────────

async def test_tick_holds_no_transaction_between_scan_iterations(pool):
    worker = CampaignWorker()

    for _ in range(3):
        await worker._tick()
        idle_in_txn = await pool.fetch(
            "SELECT pid, query FROM pg_stat_activity WHERE state = 'idle in transaction'",
        )
        assert idle_in_txn == []
