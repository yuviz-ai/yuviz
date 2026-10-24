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
    await _reconcile(redis_raw)
    assert sends == [att["idempotency_key"]]
    assert (await _attempt(scratch_pool, att["idempotency_key"]))["provider_call_id"] == "vendor-1"


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
