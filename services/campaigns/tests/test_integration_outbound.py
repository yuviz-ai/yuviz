"""Worker, stream consumer, attempts and the real DialRequestClient on real Postgres and Redis.

The fakes are Kamailio (an httpx MockTransport) and FreeSWITCH (functions that GETDEL the intent, set the claim
and XADD events exactly as the Lua does); the provider is the telephony client, monkeypatched."""

from __future__ import annotations

import json

import httpx
import pytest

from libs.telephony_sdk import campaign_events
from services.campaigns import attempts, outcome_events, telephony_originate
from services.campaigns.dial_request import DialRequestClient
from services.campaigns.tests import seed, world as w
from services.campaigns.worker import CampaignWorker


class Rig:
    """Fake Kamailio plus fake FreeSWITCH around the real DialRequestClient."""

    def __init__(self, redis_client):
        self.redis = redis_client
        self.requests = 0
        self.lose_reply = False
        self.dial = DialRequestClient("http://kamailio.test/campaign-dial", b"hmac", redis_client)
        self.dial._http = httpx.AsyncClient(transport=httpx.MockTransport(self._kamailio))

    def _kamailio(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.lose_reply:
            raise httpx.ReadTimeout("reply lost", request=request)
        return httpx.Response(200, json={"result": "accepted"})

    def worker(self) -> CampaignWorker:
        return CampaignWorker(dial_client=self.dial, redis_client=self.redis, billing=w.FakeBilling(), egress_secret=b"e")

    async def fs_consume(self, key: str) -> bool:
        """FreeSWITCH: GETDEL the intent, then SET NX the claim. False when nothing was dialed."""
        if await self.redis.getdel(f"campaign:intent:{key}") is None:
            return False
        return bool(await self.redis.set(f"campaign:claim:{key}", b"fs1", nx=True, ex=105))

    async def fs_event(self, row, kind, *, cause="", answered=False):
        fields = campaign_events.encode(
            source="fs", kind=kind, key=row["idempotency_key"], tenant_id=str(row["tenant_id"]), provider_call_id=None,
            call_session_id=row["call_session_id"], cause=cause, answered=answered, billable_ms=None,
        )
        await self.redis.xadd(campaign_events.STREAM_FS, fields)


@pytest.fixture
async def rig(scratch_redis_raw):
    await outcome_events.ensure_groups(scratch_redis_raw)
    return Rig(scratch_redis_raw)


async def _world(pool, **kwargs):
    async with pool.acquire() as conn:
        world = await w.make(conn, **kwargs)
        contact = await seed.contact(conn, world["campaign"]["id"])
    return world, contact


async def _tick_campaign(pool, rig_, world, worker=None):
    worker = worker or rig_.worker()
    await worker._dial_next(await w.refreshed(pool, world["campaign"]["id"]))
    return worker


async def _one_attempt(pool, world, n=0):
    return (await w.attempt_rows(pool, world["campaign"]["id"]))[n]


async def _age(pool, attempt, *, seconds, expired=False):
    await pool.execute(
        "UPDATE campaign_attempts SET created_at = now() - make_interval(secs => $2), "
        "deadline_at = now() + make_interval(secs => $3) WHERE id = $1",
        attempt["id"], seconds, -5 if expired else 120,
    )


# ── 1. crash matrix ──

async def test_crash_after_the_pending_commit_sends_nothing_and_fails_at_the_deadline(scratch_pool, rig):
    world, contact = await _world(scratch_pool, retry_gap_minutes=1, max_attempts=2)
    async with scratch_pool.acquire() as conn:
        from services.campaigns import campaign_contacts
        claim = await campaign_contacts.claim_next_due(
            world["campaign"], contact_id=contact["id"], path="sip", provider=None, cps_scope="sip_trunk:pstn",
        )
    attempt = claim.attempt  # the worker dies here: committed pending, nothing sent
    await _age(scratch_pool, attempt, seconds=130, expired=True)
    worker = rig.worker()
    await worker._reconcile()
    row = await _one_attempt(scratch_pool, world)
    assert rig.requests == 0 and (row["outcome"], row["closed_by_deadline"], row["send_count"]) == ("failed", True, 1)
    assert (await w.contact_row(scratch_pool, contact["id"]))["status"] == "pending"


async def test_lost_invite_is_resent_exactly_once_then_the_deadline_path(scratch_pool, rig):
    world, contact = await _world(scratch_pool, retry_gap_minutes=1, max_attempts=2)
    await _tick_campaign(scratch_pool, rig, world)
    attempt = await _one_attempt(scratch_pool, world)
    rig.lose_reply = True
    await scratch_pool.execute("UPDATE campaign_attempts SET state = 'pending' WHERE id = $1", attempt["id"])
    await _age(scratch_pool, attempt, seconds=30)
    worker = rig.worker()
    before = rig.requests
    await worker._reconcile()
    await worker._reconcile()
    assert rig.requests == before + 1   # the intent is never consumed: one re-send, then reserve_send refuses
    row = await _one_attempt(scratch_pool, world)
    assert row["send_count"] == attempts.MAX_SENDS
    await _age(scratch_pool, row, seconds=130, expired=True)
    await worker._reconcile()
    assert (await _one_attempt(scratch_pool, world))["closed_by_deadline"] is True


async def test_reply_lost_but_freeswitch_dialed_means_no_second_dial_request(scratch_pool, rig):
    world, contact = await _world(scratch_pool)
    rig.lose_reply = True
    await _tick_campaign(scratch_pool, rig, world)
    attempt = await _one_attempt(scratch_pool, world)
    assert attempt["state"] == "pending" and rig.requests == 1
    assert await rig.fs_consume(attempt["idempotency_key"])
    await _age(scratch_pool, attempt, seconds=30)
    await rig.worker()._reconcile()
    assert rig.requests == 1   # a claim exists: wait, never a second request
    intent = campaign_intent(attempt)
    assert await rig.dial.send(intent, first=False) == "duplicate" and rig.requests == 1


def campaign_intent(attempt):
    from services.campaigns.dial_request import build_intent
    return build_intent(
        key=attempt["idempotency_key"], tenant_id=str(attempt["tenant_id"]), campaign_id=str(attempt["campaign_id"]),
        contact_id=str(attempt["contact_id"]), call_uuid=attempt["call_session_id"], callee=attempt["callee"],
        caller_id=attempt["caller_id"], tenant_slug="t", agent_slug="a", max_call_s=attempt["max_call_s"], egress_secret=b"e",
    )


async def test_delayed_restart_after_claim_expiry_resends_nothing_and_retries_with_a_new_key(scratch_pool, rig):
    world, contact = await _world(scratch_pool, retry_gap_minutes=1, max_attempts=2)
    rig.lose_reply = True   # killed after send, before mark_sent
    await _tick_campaign(scratch_pool, rig, world)
    attempt = await _one_attempt(scratch_pool, world)
    assert await rig.fs_consume(attempt["idempotency_key"])   # FS dials; its hangup event is dropped
    await rig.redis.delete(f"campaign:claim:{attempt['idempotency_key']}")   # the 105 s claim has lapsed
    await _age(scratch_pool, attempt, seconds=130, expired=True)
    before = rig.requests
    restarted = rig.worker()
    await restarted._reconcile()
    row = await _one_attempt(scratch_pool, world)
    assert rig.requests == before and row["send_count"] == 1
    assert (row["outcome"], row["closed_by_deadline"]) == ("failed", True)
    await scratch_pool.execute("UPDATE campaign_contacts SET next_attempt_at = now() WHERE id = $1", contact["id"])
    await _tick_campaign(scratch_pool, rig, world, restarted)
    keys = [a["idempotency_key"] for a in await w.attempt_rows(scratch_pool, world["campaign"]["id"])]
    assert keys == [f"{contact['id']}_1", f"{contact['id']}_2"]


async def test_answered_call_is_not_redialed_and_ends_from_its_call_row(scratch_pool, rig):
    world, contact = await _world(scratch_pool)
    worker = await _tick_campaign(scratch_pool, rig, world)
    attempt = await _one_attempt(scratch_pool, world)
    await rig.fs_consume(attempt["idempotency_key"])
    async with scratch_pool.acquire() as conn:
        await w.add_call(conn, world["tenant_id"], attempt["call_session_id"], ended=False)
    await worker._reconcile()
    assert (await _one_attempt(scratch_pool, world))["outcome"] == "answered"
    await scratch_pool.execute("UPDATE calls SET ended_at = now() WHERE session_id = $1", attempt["call_session_id"])
    await worker._reconcile()
    await worker._dial_next(await w.refreshed(scratch_pool, world["campaign"]["id"]))
    assert (await _one_attempt(scratch_pool, world))["outcome"] == "ended" and rig.requests == 1
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["status"] == "completed"


async def test_events_left_unacked_by_a_dead_consumer_are_applied_after_the_idle_window(scratch_pool, rig):
    world, contact = await _world(scratch_pool)
    await _tick_campaign(scratch_pool, rig, world)
    attempt = await _one_attempt(scratch_pool, world)
    await rig.fs_event(attempt, "hangup", cause="USER_BUSY")
    delivered = await rig.redis.xreadgroup(campaign_events.GROUP, "dead-consumer", {campaign_events.STREAM_FS: ">"})
    entry_id = delivered[0][1][0][0]
    await rig.redis.xclaim(
        campaign_events.STREAM_FS, campaign_events.GROUP, "dead-consumer", 0, [entry_id], idle=outcome_events.RECLAIM_IDLE_MS + 5000,
    )
    assert (await _one_attempt(scratch_pool, world))["outcome"] is None
    await rig.worker()._drain_events()
    assert (await _one_attempt(scratch_pool, world))["outcome"] == "busy"
    assert (await rig.redis.xpending(campaign_events.STREAM_FS, campaign_events.GROUP))["pending"] == 0


# ── 2. late answer, declined retry gap ──

async def test_late_answer_corrects_a_deadline_failure_without_a_second_call(scratch_pool, rig):
    world, contact = await _world(scratch_pool, retry_gap_minutes=1, max_attempts=3)
    worker = await _tick_campaign(scratch_pool, rig, world)
    attempt = await _one_attempt(scratch_pool, world)
    await _age(scratch_pool, attempt, seconds=130, expired=True)
    await worker._reconcile()
    assert (await _one_attempt(scratch_pool, world))["closed_by_deadline"] is True
    await rig.fs_event(attempt, "answered", answered=True)
    await worker._drain_events()
    row = await _one_attempt(scratch_pool, world)
    assert row["outcome"] == "answered"
    assert (await w.contact_row(scratch_pool, contact["id"]))["status"] == "completed"
    await worker._dial_next(await w.refreshed(scratch_pool, world["campaign"]["id"]))
    assert rig.requests == 1 and len(await w.attempt_rows(scratch_pool, world["campaign"]["id"])) == 1


async def test_declined_retries_after_the_retry_gap_not_the_pacing_interval_and_stops_at_max_attempts(scratch_pool, rig):
    world, contact = await _world(scratch_pool, retry_gap_minutes=7, max_attempts=2)
    worker = await _tick_campaign(scratch_pool, rig, world)
    first = await _one_attempt(scratch_pool, world)
    await rig.fs_event(first, "hangup", cause="CALL_REJECTED")
    await worker._drain_events()
    row = await _one_attempt(scratch_pool, world)
    gap = await scratch_pool.fetchval(
        "SELECT extract(epoch FROM (next_attempt_at - $2::timestamptz)) / 60 FROM campaign_contacts WHERE id = $1",
        contact["id"], row["resolved_at"],
    )
    assert row["outcome"] == "declined" and gap == pytest.approx(7, abs=0.1)

    await scratch_pool.execute("UPDATE campaign_contacts SET next_attempt_at = now() WHERE id = $1", contact["id"])
    await _tick_campaign(scratch_pool, rig, world, worker)
    second = await _one_attempt(scratch_pool, world, 1)
    await rig.fs_event(second, "hangup", cause="CALL_REJECTED")
    await worker._drain_events()
    assert (await w.contact_row(scratch_pool, contact["id"]))["status"] == "declined"
    await worker._dial_next(await w.refreshed(scratch_pool, world["campaign"]["id"]))
    assert len(await w.attempt_rows(scratch_pool, world["campaign"]["id"])) == 2


# ── 3. live-signal guard ──

async def test_live_claim_keeps_its_slot_past_the_deadline_until_the_claim_expires(scratch_pool, rig):
    world, contact = await _world(scratch_pool, retry_gap_minutes=1, max_attempts=2)
    worker = await _tick_campaign(scratch_pool, rig, world)
    attempt = await _one_attempt(scratch_pool, world)
    await rig.fs_consume(attempt["idempotency_key"])
    await _age(scratch_pool, attempt, seconds=130, expired=True)
    await worker._reconcile()
    assert (await _one_attempt(scratch_pool, world))["state"] == "sent"
    await rig.redis.delete(f"campaign:claim:{attempt['idempotency_key']}")
    await worker._reconcile()
    assert (await _one_attempt(scratch_pool, world))["outcome"] == "failed"


# ── 4. webhook trust (consumer side; the signature and replay checks are in telephony's tests) ──

async def _cpaas_world(pool, monkeypatch, api_status, **api_fields):
    world, contact = await _world(pool, provider="fake", max_attempts=1)
    async with pool.acquire() as conn:
        _, attempt = await w.open_attempt(conn, world, path="cpaas", state="sent", provider="fake")

    async def call_status(**kw):
        return {"status": api_status, "to": attempt["callee"], "from": attempt["caller_id"], **api_fields}

    monkeypatch.setattr(telephony_originate, "call_status", call_status)
    return world, attempt


async def _cpaas_hangup(rig_, attempt, *, tenant_id=None, cause="completed", provider_call_id="vendor-1"):
    fields = campaign_events.encode(
        source="cpaas", kind="hangup", key=attempt["idempotency_key"], tenant_id=str(tenant_id or attempt["tenant_id"]),
        provider_call_id=provider_call_id, call_session_id=None, cause=cause, answered=True, billable_ms=None, unconfirmed=True,
    )
    await rig_.redis.xadd(campaign_events.STREAM_CPAAS, fields)
    await rig_.worker()._drain_events()


async def test_a_tampered_webhook_status_never_overrides_the_provider_api(scratch_pool, rig, monkeypatch):
    world, attempt = await _cpaas_world(scratch_pool, monkeypatch, "busy")
    await _cpaas_hangup(rig, attempt, cause="completed")
    assert (await _one_attempt(scratch_pool, world))["outcome"] == "busy"


async def test_a_hinted_call_whose_numbers_differ_changes_nothing(scratch_pool, rig, monkeypatch):
    world, attempt = await _cpaas_world(scratch_pool, monkeypatch, "ended", to="+10000000000")
    await _cpaas_hangup(rig, attempt)
    assert (await _one_attempt(scratch_pool, world))["state"] == "sent"


async def test_a_terminal_webhook_while_the_api_says_live_changes_nothing(scratch_pool, rig, monkeypatch):
    world, attempt = await _cpaas_world(scratch_pool, monkeypatch, "live")
    await _cpaas_hangup(rig, attempt)
    assert (await _one_attempt(scratch_pool, world))["state"] == "sent"


async def test_tenant_b_naming_tenant_a_key_changes_no_row(scratch_pool, rig, monkeypatch):
    world, attempt = await _cpaas_world(scratch_pool, monkeypatch, "ended")
    async with scratch_pool.acquire() as conn:
        other = await w.make(conn)
    await _cpaas_hangup(rig, attempt, tenant_id=other["tenant_id"])
    assert (await _one_attempt(scratch_pool, world))["state"] == "sent"


# ── 5. capacity under the deployed constants ──

async def test_tenant_a_at_its_cap_does_not_block_tenant_b(scratch_pool, rig):
    async with scratch_pool.acquire() as conn:
        a = await w.make(conn, max_concurrent_calls=5)
        b = await w.make(conn, max_concurrent_calls=5)
        await conn.execute("UPDATE tenants SET max_outbound_concurrent_calls = 1 WHERE id = $1", a["tenant_id"])
        await w.open_attempt(conn, a)
        await seed.contact(conn, a["campaign"]["id"])
        await seed.contact(conn, b["campaign"]["id"])
    worker = rig.worker()
    for world in (a, b):
        await worker._dial_next(await w.refreshed(scratch_pool, world["campaign"]["id"]))
    assert (await w.refreshed(scratch_pool, a["campaign"]["id"]))["wait_reason"] == "tenant_cap"
    assert len(await w.attempt_rows(scratch_pool, a["campaign"]["id"])) == 1
    assert len(await w.attempt_rows(scratch_pool, b["campaign"]["id"])) == 1


async def test_platform_cap_reached_leaves_due_contacts_pending_and_unconsumed(scratch_pool, rig):
    async with scratch_pool.acquire() as conn:
        world = await w.make(conn)
        contact = await seed.contact(conn, world["campaign"]["id"])
        await w.open_attempt(conn, world)
        await conn.execute("UPDATE platform_outbound_limits SET max_outbound_concurrent_calls = 1")
    try:
        await _tick_campaign(scratch_pool, rig, world)
    finally:
        await scratch_pool.execute("UPDATE platform_outbound_limits SET max_outbound_concurrent_calls = 225")
    row = await w.contact_row(scratch_pool, contact["id"])
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["wait_reason"] == "platform_cap"
    assert (row["status"], row["attempt_count"]) == ("pending", 0)


async def test_trunk_cps_is_never_exceeded_across_two_tenants(scratch_pool, rig):
    async with scratch_pool.acquire() as conn:
        worlds = [await w.make(conn) for _ in range(2)]
        for world in worlds:
            for _ in range(4):
                await seed.contact(conn, world["campaign"]["id"])
        await conn.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ('sip_trunk:pstn', 2) ON CONFLICT (scope) DO UPDATE SET cps = 2")
        await conn.execute("UPDATE campaign_attempts SET last_send_at = now() - interval '1 hour'")
    try:
        worker = rig.worker()
        for _ in range(4):
            for world in worlds:
                await worker._dial_next(await w.refreshed(scratch_pool, world["campaign"]["id"]))
        sends = [
            r["last_send_at"] for world in worlds for r in await w.attempt_rows(scratch_pool, world["campaign"]["id"])
        ]
        assert len(sends) > 0
        windows = [sum(1 for t in sends if 0 <= (t - s).total_seconds() < 1) for s in sends]
        assert max(windows) <= 2
    finally:
        await scratch_pool.execute("DELETE FROM outbound_cps_limits")


async def test_restart_rebuilds_in_flight_from_rows_and_exceeds_no_limit(scratch_pool, rig):
    async with scratch_pool.acquire() as conn:
        world = await w.make(conn, max_concurrent_calls=2)
        for _ in range(2):
            await w.open_attempt(conn, world)
        for _ in range(3):
            await seed.contact(conn, world["campaign"]["id"])
    fresh = rig.worker()   # a restarted process has no memory
    await fresh._dial_next(await w.refreshed(scratch_pool, world["campaign"]["id"]))
    open_n = await scratch_pool.fetchval(
        "SELECT count(*) FROM campaign_attempts WHERE campaign_id = $1 AND (state <> 'resolved' OR outcome = 'answered')",
        world["campaign"]["id"],
    )
    assert open_n == 2 and rig.requests == 0
