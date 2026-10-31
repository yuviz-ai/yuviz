"""One test per row of the Path A/B reconcile table, the deadline and live-signal rules, and the completion rule."""

from __future__ import annotations

import pytest

from libs.telephony_sdk import campaign_events
from services.campaigns import attempts, campaigns, outcome_events, telephony_originate
from services.campaigns.tests import world as w
from libs.telephony_sdk.exceptions import TelephonyRateLimited


@pytest.fixture
async def redis_raw(scratch_redis_raw):
    return scratch_redis_raw


async def _setup(pool, **kwargs):
    async with pool.acquire() as conn:
        return await w.make(conn, **kwargs)


async def _reconcile(redis_client, dial=None):
    wk = w.worker(redis_client, dial)
    await wk._reconcile()
    return wk


async def _attempt(pool, key):
    return dict(await pool.fetchrow("SELECT * FROM campaign_attempts WHERE idempotency_key = $1", key))


async def test_a_pending_with_a_live_call_row_becomes_answered(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world)
        await w.add_call(conn, world["tenant_id"], att["call_session_id"], ended=False)
    await _reconcile(redis_raw)
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["outcome"] == "answered"


async def test_a_pending_with_an_ended_call_row_becomes_ended(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world)
        await w.add_call(conn, world["tenant_id"], att["call_session_id"], ended=True)
    await _reconcile(redis_raw)
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["outcome"] == "ended"


async def test_a_pending_with_a_claim_and_no_call_row_waits_and_sends_nothing(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, age_s=30)
    await redis_raw.set(f"campaign:claim:{att['idempotency_key']}", b"x")
    await redis_raw.set(f"campaign:intent:{att['idempotency_key']}", b"{}")
    dial = w.FakeDial()
    await _reconcile(redis_raw, dial)
    row = await _attempt(scratch_pool, att["idempotency_key"])
    assert (row["state"], row["send_count"], dial.sent) == ("pending", 1, [])


async def test_a_pending_with_no_claim_and_the_intent_present_is_resent_once_with_the_same_key(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, age_s=30)
    await redis_raw.set(f"campaign:intent:{att['idempotency_key']}", b"{}")
    dial = w.FakeDial("accepted")
    await _reconcile(redis_raw, dial)
    row = await _attempt(scratch_pool, att["idempotency_key"])
    assert [(i.key, first) for i, first in dial.sent] == [(att["idempotency_key"], False)]
    assert (row["state"], row["send_count"]) == ("sent", 2)


async def test_a_pending_younger_than_ten_seconds_is_not_resent(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, age_s=3)
    await redis_raw.set(f"campaign:intent:{att['idempotency_key']}", b"{}")
    dial = w.FakeDial()
    await _reconcile(redis_raw, dial)
    assert dial.sent == []


async def test_a_pending_with_the_intent_gone_sends_nothing_and_fails_at_the_deadline(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        contact, att = await w.open_attempt(conn, world, age_s=30)
        _, late = await w.open_attempt(conn, world, age_s=130, deadline_in_s=-5)
    dial = w.FakeDial()
    await _reconcile(redis_raw, dial)
    assert dial.sent == []
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["state"] == "pending"
    row = await _attempt(scratch_pool, late["idempotency_key"])
    assert (row["outcome"], row["closed_by_deadline"]) == ("failed", True)


async def test_a_lapsed_claim_past_the_resend_window_sends_nothing_under_that_key(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, age_s=70)
    await redis_raw.set(f"campaign:intent:{att['idempotency_key']}", b"{}")
    dial = w.FakeDial()
    await _reconcile(redis_raw, dial)
    row = await _attempt(scratch_pool, att["idempotency_key"])
    assert (dial.sent, row["send_count"], row["state"]) == ([], 1, "pending")


async def test_wait_cps_leaves_the_attempt_untouched(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, age_s=30)
        await conn.execute(
            "INSERT INTO outbound_cps_limits (scope, cps) VALUES ('sip_trunk:pstn', 1) ON CONFLICT (scope) DO UPDATE SET cps = 1"
        )
        await conn.execute("UPDATE campaign_attempts SET last_send_at = clock_timestamp() WHERE id = $1", att["id"])
    await redis_raw.set(f"campaign:intent:{att['idempotency_key']}", b"{}")
    dial = w.FakeDial()
    try:
        await _reconcile(redis_raw, dial)
        row = await _attempt(scratch_pool, att["idempotency_key"])
        assert (dial.sent, row["send_count"], row["state"]) == ([], 1, "pending")
    finally:
        await scratch_pool.execute("DELETE FROM outbound_cps_limits")


async def test_a_sent_attempt_with_no_claim_and_no_call_does_nothing_until_the_deadline(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, early = await w.open_attempt(conn, world, state="sent")
        _, late = await w.open_attempt(conn, world, state="sent", deadline_in_s=-5)
    await _reconcile(redis_raw)
    assert (await _attempt(scratch_pool, early["idempotency_key"]))["state"] == "sent"
    assert (await _attempt(scratch_pool, late["idempotency_key"]))["outcome"] == "failed"


async def test_a_live_claim_at_the_deadline_is_not_failed_and_after_expiry_it_retries_with_a_new_key(scratch_pool, redis_raw):
    world = await _setup(scratch_pool, max_attempts=3, retry_gap_minutes=1)
    async with scratch_pool.acquire() as conn:
        contact, att = await w.open_attempt(conn, world, state="sent", deadline_in_s=-5)
    key = att["idempotency_key"]
    await redis_raw.set(f"campaign:claim:{key}", b"x")
    await _reconcile(redis_raw)
    assert (await _attempt(scratch_pool, key))["state"] == "sent"
    in_flight = await scratch_pool.fetchval(
        "SELECT count(*) FROM campaign_attempts WHERE campaign_id = $1 AND state IN ('pending', 'sent')",
        world["campaign"]["id"],
    )
    assert in_flight == 1

    await redis_raw.delete(f"campaign:claim:{key}")
    await _reconcile(redis_raw)
    assert (await _attempt(scratch_pool, key))["outcome"] == "failed"
    assert (await scratch_pool.fetchval("SELECT status FROM campaign_contacts WHERE id = $1", contact["id"])) == "pending"

    await scratch_pool.execute("UPDATE campaign_contacts SET next_attempt_at = now() WHERE id = $1", contact["id"])
    wk = w.worker(redis_raw)
    await wk._dial_next(await w.refreshed(scratch_pool, world["campaign"]["id"]))
    keys = {a["idempotency_key"] for a in await w.attempt_rows(scratch_pool, world["campaign"]["id"])}
    assert {key, attempts.idempotency_key(contact["id"], 2)} <= keys


async def test_b_pending_reconcile_placed_adopts_the_provider_call(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, path="cpaas", provider="fake", age_s=30)

    async def placed(**kw):
        return {"outcome": "placed", "provider_call_id": "vendor-9"}

    monkeypatch.setattr(telephony_originate, "reconcile_call", placed)
    await _reconcile(redis_raw)
    row = await _attempt(scratch_pool, att["idempotency_key"])
    assert (row["state"], row["provider_call_id"]) == ("sent", "vendor-9")


async def test_b_pending_not_placed_sends_once_and_unknown_sends_nothing(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, path="cpaas", provider="fake", age_s=30)
    scripted = {"outcome": "unknown", "provider_call_id": None}
    sends = []

    async def reconcile(**kw):
        return scripted

    async def originate(**kw):
        sends.append(kw["idempotency_key"])
        return "vendor-1"

    monkeypatch.setattr(telephony_originate, "reconcile_call", reconcile)
    monkeypatch.setattr(telephony_originate, "originate_call", originate)
    await _reconcile(redis_raw)
    assert sends == [] and (await _attempt(scratch_pool, att["idempotency_key"]))["state"] == "pending"

    scripted["outcome"] = "not_placed"
    await _reconcile(redis_raw)
    assert sends == []
    await redis_raw.delete(f"campaign:claim:reconcile:{att['idempotency_key']}")
    await _reconcile(redis_raw)
    await _reconcile(redis_raw)
    assert sends == [att["idempotency_key"]]
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["provider_call_id"] == "vendor-1"


async def test_b_reconcile_asks_the_provider_at_most_once_per_window(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, path="cpaas", provider="fake", age_s=30)
    calls = []

    async def reconcile(**kw):
        calls.append(kw)
        return {"outcome": "unknown", "provider_call_id": None}

    monkeypatch.setattr(telephony_originate, "reconcile_call", reconcile)
    for _ in range(3):
        await _reconcile(redis_raw)
    assert len(calls) == 1
    assert 0 < await redis_raw.ttl(f"campaign:claim:reconcile:{att['idempotency_key']}") <= 20
    async with scratch_pool.acquire() as conn:
        await conn.execute(
            "UPDATE campaign_attempts SET state = 'resolved', outcome = 'failed', resolved_at = now() WHERE id = $1",
            att["id"],
        )


async def test_b_unconfirmed_resend_is_capped_and_a_young_attempt_is_left_alone(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, young = await w.open_attempt(conn, world, path="cpaas", provider="fake")
        _, old = await w.open_attempt(conn, world, path="cpaas", provider="fake", age_s=30)
    reconciles, sends = [], []

    async def reconcile(**kw):
        reconciles.append(kw["idempotency_key"])
        return {"outcome": "not_placed", "provider_call_id": None}

    async def originate(**kw):
        sends.append(kw["idempotency_key"])
        raise telephony_originate.TelephonyOriginatePending("202")

    monkeypatch.setattr(telephony_originate, "reconcile_call", reconcile)
    monkeypatch.setattr(telephony_originate, "originate_call", originate)
    for _ in range(5):
        await _reconcile(redis_raw)
    assert set(reconciles) == {old["idempotency_key"]} and len(reconciles) <= attempts.MAX_SENDS
    assert sends == [old["idempotency_key"]]
    assert (await _attempt(scratch_pool, old["idempotency_key"]))["send_count"] == attempts.MAX_SENDS


async def test_b_rate_limited_send_holds_the_attempt_back_and_moves_the_deadline(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, path="cpaas", provider="fake", deadline_in_s=5)

    async def limited(**kw):
        raise TelephonyRateLimited("slow down", retry_after=40)

    monkeypatch.setattr(telephony_originate, "originate_call", limited)
    wk = w.worker(redis_raw)
    await wk._send_cpaas({**att, "tenant_slug": "t", "agent_slug": "a"})
    row = await _attempt(scratch_pool, att["idempotency_key"])
    gap = await scratch_pool.fetchval(
        "SELECT extract(epoch FROM (deadline_at - not_before)) FROM campaign_attempts WHERE id = $1", att["id"],
    )
    assert row["state"] == "pending" and row["send_count"] == 1
    assert gap == pytest.approx(attempts.DEADLINE_S, abs=1)
    assert await scratch_pool.fetchval("SELECT not_before > now() + interval '30 seconds' FROM campaign_attempts WHERE id = $1", att["id"])


@pytest.mark.parametrize("status, expected", [("live", "sent"), ("busy", "resolved")])
async def test_b_sent_past_deadline_follows_the_provider_status(scratch_pool, redis_raw, monkeypatch, status, expected):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(
            conn, world, path="cpaas", state="sent", provider="fake", provider_call_id="vendor-2", deadline_in_s=-5,
        )

    async def call_status(**kw):
        return {"status": status, "to": None, "from": None}

    monkeypatch.setattr(telephony_originate, "call_status", call_status)
    await _reconcile(redis_raw)
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["state"] == expected


async def test_b_provider_error_past_deadline_leaves_the_attempt_alone(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(
            conn, world, path="cpaas", state="sent", provider="fake", provider_call_id="vendor-2", deadline_in_s=-5,
        )

    async def down(**kw):
        raise telephony_originate.TelephonyOriginateError("down")

    monkeypatch.setattr(telephony_originate, "call_status", down)
    await _reconcile(redis_raw)
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["state"] == "sent"


async def test_b_sent_with_no_call_evidence_fails_at_the_deadline_only_when_status_is_not_live(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(
            conn, world, path="cpaas", state="sent", provider="fake", provider_call_id="vendor-3", deadline_in_s=-5,
        )

    async def unknown(**kw):
        return {"status": "unknown", "to": None, "from": None}

    monkeypatch.setattr(telephony_originate, "call_status", unknown)
    await _reconcile(redis_raw)
    row = await _attempt(scratch_pool, att["idempotency_key"])
    assert (row["outcome"], row["closed_by_deadline"]) == ("failed", True)


async def test_answered_ends_from_the_call_row_or_after_the_agent_max_plus_300s(scratch_pool, redis_raw):
    world = await _setup(scratch_pool, agent_max_call_s=60)
    async with scratch_pool.acquire() as conn:
        _, ended = await w.open_attempt(conn, world, state="sent")
        _, overdue = await w.open_attempt(conn, world, state="sent")
        _, ongoing = await w.open_attempt(conn, world, state="sent")
        for a, ago in ((ended, 10), (overdue, 400), (ongoing, 10)):
            await conn.execute(
                "UPDATE campaign_attempts SET state = 'resolved', outcome = 'answered', resolved_at = now(), "
                "answered_at = now() - make_interval(secs => $2) WHERE id = $1", a["id"], ago,
            )
        await w.add_call(conn, world["tenant_id"], ended["call_session_id"], ended=True)
        await w.add_call(conn, world["tenant_id"], ongoing["call_session_id"], ended=False)
    await _reconcile(redis_raw)
    rows = {k: await _attempt(scratch_pool, a["idempotency_key"]) for k, a in
            (("ended", ended), ("overdue", overdue), ("ongoing", ongoing))}
    assert rows["ended"]["outcome"] == "ended"
    assert (rows["overdue"]["outcome"], rows["overdue"]["failure_reason"]) == ("ended", "end_not_observed")
    assert rows["ongoing"]["outcome"] == "answered"


async def test_drain_events_applies_a_freeswitch_hangup(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, state="sent")
    fields = campaign_events.encode(
        source="fs", kind="hangup", key=att["idempotency_key"], tenant_id=str(world["tenant_id"]),
        provider_call_id=None, call_session_id=att["call_session_id"], cause="USER_BUSY", answered=False, billable_ms=None,
    )
    await outcome_events.ensure_groups(redis_raw)
    await redis_raw.xadd(campaign_events.STREAM_FS, fields)
    await w.worker(redis_raw)._drain_events()
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["outcome"] == "busy"


async def test_completion_waits_for_an_answered_call_to_end(scratch_pool, redis_raw):
    world = await _setup(scratch_pool)
    async with scratch_pool.acquire() as conn:
        contact, att = await w.open_attempt(conn, world, state="sent")
        await conn.execute(
            "UPDATE campaign_attempts SET state = 'resolved', outcome = 'answered', resolved_at = now(), answered_at = now() WHERE id = $1",
            att["id"],
        )
        await conn.execute("UPDATE campaign_contacts SET status = 'completed' WHERE id = $1", contact["id"])
    wk = w.worker(redis_raw)
    campaign = await w.refreshed(scratch_pool, world["campaign"]["id"])
    await wk._dial_next(campaign)
    assert (await w.refreshed(scratch_pool, campaign["id"]))["status"] == "running"
    await scratch_pool.execute(
        "UPDATE campaign_attempts SET outcome = 'ended', ended_at = now() WHERE id = $1", att["id"],
    )
    await wk._dial_next(campaign)
    assert (await w.refreshed(scratch_pool, campaign["id"]))["status"] == "completed"
    assert await campaigns.is_finished(campaign["id"])


async def test_b_a_rate_limited_attempt_is_resent_with_the_same_key_once_its_hold_passes(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake", max_attempts=1)
    async with scratch_pool.acquire() as conn:
        contact, att = await w.open_attempt(conn, world, path="cpaas", provider="fake")
    outcomes, reconciles, sends = [TelephonyRateLimited("slow down", retry_after=1), "vendor-7"], [], []

    async def originate(**kw):
        sends.append(kw["idempotency_key"])
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def reconcile(**kw):
        reconciles.append(kw["idempotency_key"])
        return {"outcome": "unknown", "provider_call_id": None}

    monkeypatch.setattr(telephony_originate, "originate_call", originate)
    monkeypatch.setattr(telephony_originate, "reconcile_call", reconcile)
    wk = w.worker(redis_raw)
    await wk._send_cpaas({**att, "tenant_slug": "t", "agent_slug": "a"})
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["rate_limited"] is True

    await scratch_pool.execute("UPDATE campaign_attempts SET not_before = now() - interval '1 second' WHERE id = $1", att["id"])
    await wk._reconcile()
    row = await _attempt(scratch_pool, att["idempotency_key"])
    assert sends == [att["idempotency_key"]] * 2 and reconciles == []
    assert (row["state"], row["provider_call_id"], row["rate_limited"]) == ("sent", "vendor-7", False)
    assert (await w.contact_row(scratch_pool, contact["id"]))["status"] == "calling"


async def test_b_a_rate_limited_attempt_waits_out_its_hold(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, path="cpaas", provider="fake", age_s=30)
    await scratch_pool.execute(
        "UPDATE campaign_attempts SET rate_limited = true, not_before = now() + interval '30 seconds' WHERE id = $1", att["id"],
    )
    sends = []

    async def originate(**kw):
        sends.append(kw["idempotency_key"])
        return "vendor-8"

    monkeypatch.setattr(telephony_originate, "originate_call", originate)
    await _reconcile(redis_raw)
    assert sends == [] and (await _attempt(scratch_pool, att["idempotency_key"]))["state"] == "pending"


async def _held(pool, world, **attempt_sql):
    async with pool.acquire() as conn:
        contact, att = await w.open_attempt(conn, world, path="cpaas", provider="fake", age_s=30)
    await pool.execute(
        "UPDATE campaign_attempts SET rate_limited = true, not_before = now() - interval '1 second', send_count = $2 "
        "WHERE id = $1", att["id"], attempt_sql.get("send_count", 1),
    )
    return contact, att


def _no_sends(monkeypatch):
    sends = []

    async def originate(**kw):
        sends.append(kw["idempotency_key"])
        return "vendor-x"

    monkeypatch.setattr(telephony_originate, "originate_call", originate)
    return sends


@pytest.mark.parametrize("setup_sql, reason", [
    ("UPDATE campaigns SET status = 'paused' WHERE id = $1", "rate_limited_paused"),
    ("UPDATE campaigns SET status = 'cancelled' WHERE id = $1", "rate_limited_cancelled"),
    ("UPDATE campaigns SET calling_hours_start = to_char(now() AT TIME ZONE 'UTC' + interval '2 hours', 'HH24:MI'), "
     "calling_hours_end = to_char(now() AT TIME ZONE 'UTC' + interval '3 hours', 'HH24:MI'), "
     "calling_hours_timezone = 'UTC' WHERE id = $1", "rate_limited_calling_hours"),
])
async def test_b_a_held_attempt_is_never_sent_once_dialling_must_stop_and_its_lead_gets_the_attempt_back(
    scratch_pool, redis_raw, monkeypatch, setup_sql, reason,
):
    world = await _setup(scratch_pool, provider="fake")
    contact, att = await _held(scratch_pool, world)
    await scratch_pool.execute(setup_sql, world["campaign"]["id"])
    sends = _no_sends(monkeypatch)
    await _reconcile(redis_raw)
    row = await _attempt(scratch_pool, att["idempotency_key"])
    lead = await w.contact_row(scratch_pool, contact["id"])
    assert sends == []
    assert (row["state"], row["failure_reason"]) == ("resolved", reason)
    assert (lead["status"], lead["attempt_count"]) == ("pending", 0)


async def test_b_an_attempt_held_too_many_times_fails_instead_of_staying_open(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake", max_attempts=1)
    contact, att = await _held(scratch_pool, world, send_count=attempts.MAX_RATE_LIMITED_SENDS)
    sends = _no_sends(monkeypatch)
    await _reconcile(redis_raw)
    row = await _attempt(scratch_pool, att["idempotency_key"])
    assert sends == [] and (row["outcome"], row["failure_reason"]) == ("failed", "rate_limited")
    assert (await w.contact_row(scratch_pool, contact["id"]))["status"] == "failed"


async def test_b_a_pause_after_the_reconcile_scan_still_stops_the_held_send(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    _, att = await _held(scratch_pool, world)
    sends = _no_sends(monkeypatch)
    [row] = [r for r in await attempts.list_open() if r["id"] == att["id"]]
    await scratch_pool.execute("UPDATE campaigns SET status = 'paused' WHERE id = $1", world["campaign"]["id"])
    await w.worker(redis_raw)._reconcile_cpaas(row)
    assert sends == [] and (await _attempt(scratch_pool, att["idempotency_key"]))["state"] == "pending"


async def test_b_a_tiny_retry_after_still_holds_for_the_backoff(scratch_pool, redis_raw, monkeypatch):
    world = await _setup(scratch_pool, provider="fake")
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world, path="cpaas", provider="fake")

    async def limited(**kw):
        raise TelephonyRateLimited("slow down", retry_after=1)

    monkeypatch.setattr(telephony_originate, "originate_call", limited)
    await w.worker(redis_raw)._send_cpaas({**att, "tenant_slug": "t", "agent_slug": "a"})
    assert await scratch_pool.fetchval(
        "SELECT not_before > now() + interval '25 seconds' FROM campaign_attempts WHERE id = $1", att["id"],
    )
