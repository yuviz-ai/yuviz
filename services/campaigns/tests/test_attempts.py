from __future__ import annotations

import asyncio
from datetime import timedelta
import re
import uuid

import pytest

from services.campaigns import attempts
from services.campaigns.tests import seed

_ALL = ("answered", "ended", "busy", "no_answer", "declined", "failed")

# current attempt state -> outcomes it may move to. Anything else must match no row.
_ALLOWED = {
    "open_pending": _ALL,
    "open_sent": _ALL,
    "answered": ("ended",),
    "ended": (),
    "busy": (),
    "no_answer": (),
    "declined": (),
    "failed": (),
    "failed_deadline": ("answered", "ended"),
}


async def _make(pool, current: str, *, tenant_id=None, max_attempts: int = 3, contact_status: str = "calling"):
    async with pool.acquire() as conn:
        tenant_id = tenant_id or await seed.tenant(conn)
        camp = await seed.campaign(conn, tenant_id, max_attempts=max_attempts)
        cont = await seed.contact(conn, camp["id"], status=contact_status, attempt_count=1)
        if current.startswith("open_"):
            row = await seed.attempt(conn, camp, cont, state=current.removeprefix("open_"))
        else:
            row = await seed.attempt(
                conn, camp, cont, state="resolved", outcome=current.removesuffix("_deadline"),
                closed_by_deadline=current.endswith("_deadline"),
            )
    return camp, cont, row


async def _state(pool, attempt_id):
    return dict(await pool.fetchrow("SELECT * FROM campaign_attempts WHERE id = $1", attempt_id))


def _apply(row, outcome, **kw):
    return attempts.apply_outcome(
        tenant_id=row["tenant_id"], key=row["idempotency_key"], provider_call_id=None, outcome=outcome,
        call_session_id=None, **kw,
    )


@pytest.mark.parametrize("current", list(_ALLOWED))
async def test_outcome_transition_table(scratch_pool, current):
    moved = 0
    for new in _ALL:
        _, _, row = await _make(scratch_pool, current)
        before = await _state(scratch_pool, row["id"])
        result = await _apply(row, new)
        after = await _state(scratch_pool, row["id"])
        if new in _ALLOWED[current]:
            assert result is not None and after["outcome"] == new and after["state"] == "resolved", (current, new)
            moved += 1
        else:
            assert result is None and after == before, (current, new)
    assert moved == len(_ALLOWED[current])


async def test_foreign_tenant_changes_nothing(scratch_pool):
    _, cont, row = await _make(scratch_pool, "open_sent")
    async with scratch_pool.acquire() as conn:
        other = await seed.tenant(conn)
    before = await _state(scratch_pool, row["id"])
    result = await attempts.apply_outcome(
        tenant_id=other, key=row["idempotency_key"], provider_call_id=None, outcome="answered", call_session_id=None,
    )
    assert result is None
    assert await _state(scratch_pool, row["id"]) == before
    assert await scratch_pool.fetchval("SELECT status FROM campaign_contacts WHERE id = $1", cont["id"]) == "calling"


async def test_provider_call_id_match_is_tenant_scoped(scratch_pool):
    async with scratch_pool.acquire() as conn:
        t1, t2 = await seed.tenant(conn), await seed.tenant(conn)
        camp = await seed.campaign(conn, t1)
        cont = await seed.contact(conn, camp["id"], status="calling", attempt_count=1)
        row = await seed.attempt(conn, camp, cont, state="sent", provider_call_id="pc-1")
    miss = await attempts.apply_outcome(
        tenant_id=t2, key=None, provider_call_id="pc-1", outcome="busy", call_session_id=None,
    )
    hit = await attempts.apply_outcome(
        tenant_id=t1, key=None, provider_call_id="pc-1", outcome="busy", call_session_id=None,
    )
    assert miss is None and hit["id"] == row["id"]


async def test_declined_with_attempts_left_requeues_after_retry_gap(scratch_pool):
    camp, cont, row = await _make(scratch_pool, "open_sent", max_attempts=3)
    result = await _apply(row, "declined")
    contact = dict(await scratch_pool.fetchrow("SELECT * FROM campaign_contacts WHERE id = $1", cont["id"]))
    assert contact["status"] == "pending"
    assert contact["next_attempt_at"] - result["resolved_at"] == timedelta(minutes=60)
    assert result["outcome"] == "declined"


async def test_declined_on_last_attempt_ends_contact_declined(scratch_pool):
    _, cont, row = await _make(scratch_pool, "open_sent", max_attempts=1)
    await _apply(row, "declined")
    contact = dict(await scratch_pool.fetchrow("SELECT * FROM campaign_contacts WHERE id = $1", cont["id"]))
    assert contact["status"] == "declined" and contact["next_attempt_at"] is None


async def test_answered_completes_contact(scratch_pool):
    _, cont, row = await _make(scratch_pool, "open_sent")
    await _apply(row, "answered")
    assert await scratch_pool.fetchval("SELECT status FROM campaign_contacts WHERE id = $1", cont["id"]) == "completed"


async def test_late_answer_cancels_queued_retry(scratch_pool):
    _, cont, row = await _make(scratch_pool, "failed_deadline", contact_status="pending")
    await scratch_pool.execute(
        "UPDATE campaign_contacts SET next_attempt_at = now() + interval '1 hour' WHERE id = $1", cont["id"],
    )
    result = await _apply(row, "answered")
    contact = dict(await scratch_pool.fetchrow("SELECT * FROM campaign_contacts WHERE id = $1", cont["id"]))
    assert result["closed_by_deadline"] is False and result["failure_reason"] is None
    assert contact["status"] == "completed" and contact["next_attempt_at"] is None


async def test_late_answer_supersedes_the_next_attempt(scratch_pool):
    camp, cont, row = await _make(scratch_pool, "failed_deadline", contact_status="calling")
    async with scratch_pool.acquire() as conn:
        second = await seed.attempt(conn, camp, cont, n=2, state="sent")
    await _apply(row, "ended")
    assert (await _state(scratch_pool, second["id"]))["failure_reason"] == "superseded"
    assert await scratch_pool.fetchval("SELECT status FROM campaign_contacts WHERE id = $1", cont["id"]) == "completed"

    await _apply(second, "busy")
    assert await scratch_pool.fetchval("SELECT status FROM campaign_contacts WHERE id = $1", cont["id"]) == "completed"
    assert (await _state(scratch_pool, second["id"]))["failure_reason"] == "superseded"


async def test_late_answer_completes_contact_that_failed_on_last_attempt(scratch_pool):
    _, cont, row = await _make(scratch_pool, "open_sent", max_attempts=1)
    await _apply(row, "failed", closed_by_deadline=True)
    assert await scratch_pool.fetchval("SELECT status FROM campaign_contacts WHERE id = $1", cont["id"]) == "failed"
    await _apply(row, "answered")
    assert await scratch_pool.fetchval("SELECT status FROM campaign_contacts WHERE id = $1", cont["id"]) == "completed"


async def test_list_open_reaches_pending_attempt_beyond_the_answered_backlog(scratch_pool):
    async with scratch_pool.acquire() as conn:
        tenant_id = await seed.tenant(conn)
        camp = await seed.campaign(conn, tenant_id)
        for _ in range(3):
            cont = await seed.contact(conn, camp["id"], status="calling", attempt_count=1)
            await seed.attempt(conn, camp, cont, state="resolved", outcome="answered")
        cont = await seed.contact(conn, camp["id"], status="calling", attempt_count=1)
        pending = await seed.attempt(conn, camp, cont, state="pending")
        await conn.execute("UPDATE campaign_attempts SET deadline_at = now() - interval '1 minute' WHERE id = $1", pending["id"])
    unresolved = await scratch_pool.fetchval("SELECT count(*) FROM campaign_attempts WHERE state <> 'resolved'")
    rows = await attempts.list_open(limit=unresolved)
    assert all(r["state"] != "resolved" for r in rows)
    assert next(r for r in rows if r["id"] == pending["id"])["past_deadline"] is True


async def test_concurrent_apply_outcome_has_one_winner(scratch_pool):
    _, _, row = await _make(scratch_pool, "open_sent")
    results = await asyncio.gather(*(_apply(row, "busy") for _ in range(8)))
    assert sum(r is not None for r in results) == 1


async def test_mark_sent_only_from_pending(scratch_pool):
    _, _, row = await _make(scratch_pool, "open_pending")
    assert await attempts.mark_sent(row["id"], provider_call_id="pc-9") is True
    assert await attempts.mark_sent(row["id"], provider_call_id="pc-9") is False
    assert (await _state(scratch_pool, row["id"]))["provider_call_id"] == "pc-9"


async def test_reserve_send_window_and_send_count(scratch_pool):
    camp, cont, row = await _make(scratch_pool, "open_pending")
    await scratch_pool.execute("UPDATE campaign_attempts SET send_count = 1 WHERE id = $1", row["id"])
    assert await attempts.reserve_send(row["id"], path="sip") == "ok"
    assert (await _state(scratch_pool, row["id"]))["send_count"] == 2
    assert await attempts.reserve_send(row["id"], path="sip") == "refused"  # send_count < 2 no longer holds

    await scratch_pool.execute(
        "UPDATE campaign_attempts SET send_count = 1, created_at = now() - interval '61 seconds' WHERE id = $1",
        row["id"],
    )
    assert await attempts.reserve_send(row["id"], path="sip") == "refused"  # past the re-send window
    assert await attempts.reserve_send(row["id"], path="cpaas") == "ok"      # window and cap are Path A only


async def test_reserve_send_waits_when_cps_scope_is_full(scratch_pool):
    camp, cont, row = await _make(scratch_pool, "open_pending")
    scope = f"provider_account:{uuid.uuid4()}"
    await scratch_pool.execute("UPDATE campaign_attempts SET cps_scope = $2 WHERE id = $1", row["id"], scope)
    await scratch_pool.execute("INSERT INTO outbound_cps_limits (scope, cps) VALUES ($1, 1)", scope)
    await scratch_pool.execute("UPDATE campaign_attempts SET last_send_at = clock_timestamp() WHERE id = $1", row["id"])
    before = await _state(scratch_pool, row["id"])
    assert await attempts.reserve_send(row["id"], path="cpaas") == "wait_cps"
    assert await _state(scratch_pool, row["id"]) == before


def test_idempotency_key_matches_telephony_validator():
    key = attempts.idempotency_key(uuid.uuid4(), 3)
    assert re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", key)
    assert key.endswith("_3")
