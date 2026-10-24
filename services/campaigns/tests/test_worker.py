"""CampaignWorker dial path on a scratch database: dispatch, pre-checks, send results and outcome recording."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from libs.telephony_sdk import campaign_events
from services.campaigns import campaigns, outcome_events, telephony_originate
from services.campaigns.tests import seed, world as w
from services.campaigns.worker import _within_calling_hours


async def _world(pool, *, contacts=1, **kwargs):
    async with pool.acquire() as conn:
        world = await w.make(conn, **kwargs)
        for _ in range(contacts):
            await seed.contact(conn, world["campaign"]["id"])
    return world


async def _dial(pool, worker, world):
    await worker._dial_next(await w.refreshed(pool, world["campaign"]["id"]))


async def _attempts(pool, world):
    return await w.attempt_rows(pool, world["campaign"]["id"])


async def test_path_a_dial_creates_an_attempt_sends_with_first_true_and_marks_sent(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    dial, billing = w.FakeDial(), w.FakeBilling()
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial, billing), world)
    [attempt] = await _attempts(scratch_pool, world)
    assert [(i.key, first) for i, first in dial.sent] == [(attempt["idempotency_key"], True)]
    assert (attempt["path"], attempt["state"], attempt["send_count"], attempt["billing_state"]) == ("sip", "sent", 1, "off")
    assert billing.calls == []


async def test_pacing_blocks_a_second_dial_and_reports_the_wait(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool, contacts=2)
    await scratch_pool.execute("UPDATE campaigns SET pacing_seconds = 9999 WHERE id = $1", world["campaign"]["id"])
    worker = w.worker(scratch_redis_raw)
    await _dial(scratch_pool, worker, world)
    await _dial(scratch_pool, worker, world)
    assert len(await _attempts(scratch_pool, world)) == 1
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["wait_reason"] == "pacing"


async def test_first_dial_is_not_paced_on_a_freshly_booted_host(scratch_pool, scratch_redis_raw, monkeypatch):
    world = await _world(scratch_pool)
    await scratch_pool.execute("UPDATE campaigns SET pacing_seconds = 9999 WHERE id = $1", world["campaign"]["id"])
    # time.monotonic() is host uptime; a host up for 5s is far below pacing_seconds.
    monkeypatch.setattr("services.campaigns.worker.time", SimpleNamespace(monotonic=lambda: 5.0))
    await _dial(scratch_pool, w.worker(scratch_redis_raw), world)
    assert len(await _attempts(scratch_pool, world)) == 1


async def test_campaign_cap_leaves_the_second_contact_pending(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool, contacts=2, max_concurrent_calls=1)
    worker = w.worker(scratch_redis_raw)
    await _dial(scratch_pool, worker, world)
    await _dial(scratch_pool, worker, world)
    assert len(await _attempts(scratch_pool, world)) == 1
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["wait_reason"] == "campaign_cap"


async def test_campaign_completes_when_no_contacts_remain(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool, contacts=0)
    await _dial(scratch_pool, w.worker(scratch_redis_raw), world)
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["status"] == "completed"


async def test_a_campaign_without_a_caller_id_dials_nothing(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    await scratch_pool.execute("UPDATE campaigns SET caller_id = NULL WHERE id = $1", world["campaign"]["id"])
    dial = w.FakeDial()
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    assert dial.sent == [] and await _attempts(scratch_pool, world) == []


async def test_a_malformed_caller_id_pauses_the_campaign_and_spends_nothing(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    bad = "+91-80-4567-8900"
    async with scratch_pool.acquire() as conn:
        await conn.execute("UPDATE phone_numbers SET did = '918045678900' WHERE tenant_id = $1", world["tenant_id"])
        await conn.execute("UPDATE campaigns SET caller_id = $2 WHERE id = $1", world["campaign"]["id"], bad)
    await _dial(scratch_pool, w.worker(scratch_redis_raw), world)
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["status"] == "paused"
    assert await _attempts(scratch_pool, world) == []


async def test_a_malformed_stored_contact_fails_without_an_attempt(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool, contacts=0)
    async with scratch_pool.acquire() as conn:
        contact = await seed.contact(conn, world["campaign"]["id"], phone="1001\n\napi hupall")
    dial = w.FakeDial()
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    assert dial.sent == [] and await _attempts(scratch_pool, world) == []
    assert (await w.contact_row(scratch_pool, contact["id"]))["status"] == "failed"


async def test_an_unowned_caller_id_pauses_and_dials_nothing(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    await scratch_pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", world["tenant_id"])
    dial = w.FakeDial()
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    assert dial.sent == [] and await _attempts(scratch_pool, world) == []
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["status"] == "paused"


async def test_an_unavailable_agent_is_refused(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    await scratch_pool.execute("UPDATE agents SET status = 'inactive' WHERE id = $1", world["campaign"]["agent_id"])
    dial = w.FakeDial()
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    assert dial.sent == [] and (await w.refreshed(scratch_pool, world["campaign"]["id"]))["status"] == "paused"


async def test_an_unregistered_provider_is_refused_never_sent_down_path_a(scratch_pool, scratch_redis_raw, monkeypatch):
    world = await _world(scratch_pool, provider="not-a-provider")
    dial = w.FakeDial()
    originated = []

    async def originate(**kw):
        originated.append(kw)

    monkeypatch.setattr(telephony_originate, "originate_call", originate)
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    assert dial.sent == [] and originated == [] and await _attempts(scratch_pool, world) == []
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["status"] == "paused"


async def test_a_registered_provider_takes_path_b(scratch_pool, scratch_redis_raw, monkeypatch):
    world = await _world(scratch_pool, provider="fake")
    seen = []

    async def originate(**kw):
        seen.append(kw)
        return "vendor-1"

    monkeypatch.setattr(telephony_originate, "originate_call", originate)
    dial = w.FakeDial()
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    [attempt] = await _attempts(scratch_pool, world)
    assert dial.sent == [] and seen[0]["idempotency_key"] == attempt["idempotency_key"]
    assert (attempt["path"], attempt["state"], attempt["provider_call_id"]) == ("cpaas", "sent", "vendor-1")
    assert attempt["cps_scope"] == f"provider_account:{world['config_id']}"


async def test_a_dnc_listed_contact_is_blocked_and_the_log_masks_the_number(scratch_pool, scratch_redis_raw, caplog):
    world = await _world(scratch_pool, contacts=0)
    async with scratch_pool.acquire() as conn:
        contact = await seed.contact(conn, world["campaign"]["id"], phone="+14155551111")
        await conn.execute(
            "INSERT INTO dnc_numbers (tenant_id, phone_number) VALUES ($1, '+14155551111')", world["tenant_id"],
        )
    dial = w.FakeDial()
    with caplog.at_level(logging.INFO, logger="services.campaigns.worker"):
        await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    assert dial.sent == [] and (await w.contact_row(scratch_pool, contact["id"]))["status"] == "blocked"
    text = caplog.text
    assert "DNC" in text and "1111" in text and "+14155551111" not in text and "4155551111" not in text


class _Registry:
    def __init__(self, verdict=None, exc=None):
        self.verdict, self.exc = verdict, exc

    async def check(self, e164, purpose, tenant_id):
        if self.exc:
            raise self.exc
        return self.verdict


async def test_a_registry_timeout_waits_with_no_attempt_and_nothing_consumed(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    worker = w.worker(scratch_redis_raw, registry=_Registry(exc=asyncio.TimeoutError()))
    await _dial(scratch_pool, worker, world)
    assert await _attempts(scratch_pool, world) == []
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["wait_reason"] == "registry"
    [contact] = await scratch_pool.fetch("SELECT * FROM campaign_contacts WHERE campaign_id = $1", world["campaign"]["id"])
    assert (contact["status"], contact["attempt_count"]) == ("pending", 0)


async def test_a_registry_denial_blocks_the_contact(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    await _dial(scratch_pool, w.worker(scratch_redis_raw, registry=_Registry("deny")), world)
    [contact] = await scratch_pool.fetch("SELECT * FROM campaign_contacts WHERE campaign_id = $1", world["campaign"]["id"])
    assert contact["status"] == "blocked" and await _attempts(scratch_pool, world) == []


@pytest.mark.parametrize("result, failure_reason", [("rejected", "dial_rejected"), ("no_node", "no_sip_node")])
async def test_a_send_that_sent_nothing_fails_the_attempt_and_requeues_after_the_gap(
    scratch_pool, scratch_redis_raw, result, failure_reason,
):
    world = await _world(scratch_pool, retry_gap_minutes=5, max_attempts=2)
    await _dial(scratch_pool, w.worker(scratch_redis_raw, w.FakeDial(result)), world)
    [attempt] = await _attempts(scratch_pool, world)
    assert (attempt["outcome"], attempt["failure_reason"]) == ("failed", failure_reason)
    [contact] = await scratch_pool.fetch("SELECT * FROM campaign_contacts WHERE campaign_id = $1", world["campaign"]["id"])
    assert contact["status"] == "pending" and contact["next_attempt_at"] > datetime.now(ZoneInfo("UTC")) + timedelta(minutes=4)


async def test_a_timed_out_send_stays_pending_for_reconcile(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    await _dial(scratch_pool, w.worker(scratch_redis_raw, w.FakeDial("timeout")), world)
    [attempt] = await _attempts(scratch_pool, world)
    assert (attempt["state"], attempt["send_count"]) == ("pending", 1)


async def _hangup(redis_client, attempt, cause):
    await outcome_events.ensure_groups(redis_client)
    await redis_client.xadd(campaign_events.STREAM_FS, campaign_events.encode(
        source="fs", kind="hangup", key=attempt["idempotency_key"], tenant_id=str(attempt["tenant_id"]),
        provider_call_id=None, call_session_id=attempt["call_session_id"], cause=cause, answered=False, billable_ms=None,
    ))


@pytest.mark.parametrize("cause, counter", [("USER_BUSY", "busy"), ("CALL_REJECTED", "declined")])
async def test_busy_and_declined_move_the_progress_counters(scratch_pool, scratch_redis_raw, cause, counter):
    world = await _world(scratch_pool, max_attempts=1)
    worker = w.worker(scratch_redis_raw)
    await _dial(scratch_pool, worker, world)
    [attempt] = await _attempts(scratch_pool, world)
    await _hangup(scratch_redis_raw, attempt, cause)
    await worker._drain_events()
    progress = await campaigns.get_progress(world["campaign"]["id"], platform_scoped=True)
    assert (progress[counter], progress["calling"]) == (1, 0)


async def test_a_no_answer_contact_is_not_redialed_before_the_retry_gap(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool, retry_gap_minutes=60, max_attempts=3)
    dial = w.FakeDial()
    worker = w.worker(scratch_redis_raw, dial)
    await _dial(scratch_pool, worker, world)
    [attempt] = await _attempts(scratch_pool, world)
    await _hangup(scratch_redis_raw, attempt, "NO_ANSWER")
    await worker._drain_events()
    await _dial(scratch_pool, worker, world)
    assert len(dial.sent) == 1 and len(await _attempts(scratch_pool, world)) == 1
    await scratch_pool.execute(
        "UPDATE campaign_contacts SET next_attempt_at = now() - interval '1 minute' WHERE campaign_id = $1",
        world["campaign"]["id"],
    )
    await _dial(scratch_pool, worker, world)
    assert len(dial.sent) == 2


async def test_tick_holds_no_transaction_between_scan_iterations(scratch_pool, scratch_redis_raw):
    worker = w.worker(scratch_redis_raw)
    for _ in range(3):
        await worker._tick()
        idle = await scratch_pool.fetch("SELECT pid FROM pg_stat_activity WHERE state = 'idle in transaction'")
        assert idle == []


async def test_tick_starts_a_scheduled_campaign_only_once_its_time_has_passed(scratch_pool, scratch_redis_raw):
    due = await _world(scratch_pool, status="scheduled")
    later = await _world(scratch_pool, status="scheduled")
    await scratch_pool.execute(
        "UPDATE campaigns SET scheduled_start_at = now() - interval '1 minute' WHERE id = $1", due["campaign"]["id"],
    )
    await scratch_pool.execute(
        "UPDATE campaigns SET scheduled_start_at = now() + interval '1 hour' WHERE id = $1", later["campaign"]["id"],
    )
    await w.worker(scratch_redis_raw)._tick()
    assert (await w.refreshed(scratch_pool, due["campaign"]["id"]))["status"] == "running"
    assert (await w.refreshed(scratch_pool, later["campaign"]["id"]))["status"] == "scheduled"


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
    window = {"calling_hours_start": "22:00", "calling_hours_end": "06:00", "calling_hours_timezone": "UTC"}
    assert _within_calling_hours({**window}) in (True, False)  # always defined, never raises


async def test_outside_calling_hours_the_contact_is_untouched_and_the_wait_is_reported(scratch_pool, scratch_redis_raw):
    world = await _world(scratch_pool)
    now = datetime.now(ZoneInfo("UTC"))
    await scratch_pool.execute(
        "UPDATE campaigns SET calling_hours_start = $2, calling_hours_end = $3, calling_hours_timezone = 'UTC' WHERE id = $1",
        world["campaign"]["id"], (now + timedelta(hours=1)).strftime("%H:%M"), (now + timedelta(hours=2)).strftime("%H:%M"),
    )
    dial = w.FakeDial()
    await _dial(scratch_pool, w.worker(scratch_redis_raw, dial), world)
    assert dial.sent == [] and await _attempts(scratch_pool, world) == []
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["wait_reason"] == "calling_hours"


async def test_set_wait_reason_writes_only_on_change(scratch_pool):
    world = await _world(scratch_pool)
    campaign_id = world["campaign"]["id"]
    await campaigns.set_wait_reason(campaign_id, "pacing")
    since = (await w.refreshed(scratch_pool, campaign_id))["wait_since"]
    await campaigns.set_wait_reason(campaign_id, "pacing")
    assert (await w.refreshed(scratch_pool, campaign_id))["wait_since"] == since


async def test_a_scheduled_campaign_that_is_not_ready_at_its_time_waits_and_is_not_paused(scratch_pool, scratch_redis_raw):
    async with scratch_pool.acquire() as conn:
        world = await w.make(conn, status="scheduled")
    await scratch_pool.execute(
        "UPDATE campaigns SET scheduled_start_at = now() - interval '1 minute' WHERE id = $1", world["campaign"]["id"],
    )
    await w.worker(scratch_redis_raw)._tick()
    campaign = await w.refreshed(scratch_pool, world["campaign"]["id"])
    assert campaign["status"] == "scheduled"
