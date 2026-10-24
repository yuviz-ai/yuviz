from __future__ import annotations

import pytest

from libs.telephony_sdk import campaign_events
from libs.telephony_sdk.campaign_events import GROUP, STREAM_CONV, STREAM_CPAAS, STREAM_FS
from services.campaigns import outcome_events, telephony_originate
from services.campaigns.tests import seed

CALLEE, CALLER = "+919876543210", "+918065354620"


@pytest.fixture(autouse=True)
async def _groups(scratch_redis_raw):
    await outcome_events.ensure_groups(scratch_redis_raw)


async def _attempt(pool, *, path="sip", provider=None, provider_call_id=None, state="sent", tenant_id=None):
    async with pool.acquire() as conn:
        tenant_id = tenant_id or await seed.tenant(conn)
        camp = await seed.campaign(conn, tenant_id)
        cont = await seed.contact(conn, camp["id"], status="calling", attempt_count=1)
        row = await seed.attempt(conn, camp, cont, path=path, state=state, provider_call_id=provider_call_id)
        await conn.execute(
            "UPDATE campaign_attempts SET provider = $2, callee = $3, caller_id = $4 WHERE id = $1",
            row["id"], provider, CALLEE, CALLER,
        )
    return await pool.fetchrow("SELECT * FROM campaign_attempts WHERE id = $1", row["id"])


async def _emit(client, stream, row, *, kind="hangup", cause="NORMAL_CLEARING", answered=False, tenant_id=None,
                source=None, provider_call_id=None, **extra):
    fields = campaign_events.encode(
        source=source or ("fs" if stream == STREAM_FS else "cpaas"), kind=kind, key=row["idempotency_key"],
        tenant_id=str(tenant_id or row["tenant_id"]), provider_call_id=provider_call_id,
        call_session_id=row["call_session_id"], cause=cause, answered=answered, billable_ms=None,
    )
    return await client.xadd(stream, {**fields, **extra})


async def _outcome(pool, row):
    return (await pool.fetchrow("SELECT outcome, state FROM campaign_attempts WHERE id = $1", row["id"]))["outcome"]


async def _pending(client, stream):
    return (await client.xpending(stream, GROUP))["pending"]


def _api(monkeypatch, status, *, to=CALLEE, from_=CALLER):
    calls = []

    async def fake_status(*, provider, tenant_slug, call_id):
        calls.append((provider, tenant_slug, call_id))
        if isinstance(status, Exception):
            raise status
        return {"status": status, "to": to, "from": from_}

    monkeypatch.setattr(telephony_originate, "call_status", fake_status)
    return calls


@pytest.mark.parametrize("cause, answered, expected", [
    ("USER_BUSY", False, "busy"), ("NO_ANSWER", False, "no_answer"), ("NO_USER_RESPONSE", False, "no_answer"),
    ("ALLOTTED_TIMEOUT", False, "no_answer"), ("CALL_REJECTED", False, "declined"),
    ("NETWORK_OUT_OF_ORDER", False, "failed"), ("NORMAL_CLEARING", True, "ended"),
])
async def test_fs_hangup_outcome_mapping(scratch_pool, scratch_redis_raw, cause, answered, expected):
    row = await _attempt(scratch_pool)
    await _emit(scratch_redis_raw, STREAM_FS, row, cause=cause, answered=answered)
    assert await outcome_events.drain(scratch_redis_raw, "c1") == 1
    assert await _outcome(scratch_pool, row) == expected
    assert await _pending(scratch_redis_raw, STREAM_FS) == 0


async def test_fs_answered_event_marks_answered(scratch_pool, scratch_redis_raw):
    row = await _attempt(scratch_pool)
    await _emit(scratch_redis_raw, STREAM_FS, row, kind="answered", answered=True)
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert await _outcome(scratch_pool, row) == "answered"


async def test_forged_fs_source_on_the_cpaas_and_conv_streams_changes_nothing(scratch_pool, scratch_redis_raw, monkeypatch):
    _api(monkeypatch, "live")
    sip = await _attempt(scratch_pool)
    cpaas = await _attempt(scratch_pool, path="cpaas", provider="fake")
    forged = dict(source="fs", billable_ms="999999999", answered="1")

    await _emit(scratch_redis_raw, STREAM_CPAAS, cpaas, cause="USER_BUSY", **forged)
    await _emit(scratch_redis_raw, STREAM_CPAAS, sip, cause="USER_BUSY", **forged)
    await _emit(scratch_redis_raw, STREAM_CONV, sip, cause="USER_BUSY", source="fs")
    await _emit(scratch_redis_raw, STREAM_CONV, sip, kind="answered", source="fs")
    assert await outcome_events.drain(scratch_redis_raw, "c1") == 4

    assert await _outcome(scratch_pool, sip) is None and await _outcome(scratch_pool, cpaas) is None
    assert await _pending(scratch_redis_raw, STREAM_CPAAS) == 0 and await _pending(scratch_redis_raw, STREAM_CONV) == 0
    assert (await scratch_pool.fetchval("SELECT billable_ms FROM campaign_attempts WHERE id = $1", sip["id"])) is None

    # The same entry on the stream FreeSWITCH owns does apply.
    await _emit(scratch_redis_raw, STREAM_FS, sip, cause="USER_BUSY")
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert await _outcome(scratch_pool, sip) == "busy"


async def test_webhook_completed_with_api_busy_records_busy(scratch_pool, scratch_redis_raw, monkeypatch):
    calls = _api(monkeypatch, "busy")
    row = await _attempt(scratch_pool, path="cpaas", provider="fake")
    await _emit(scratch_redis_raw, STREAM_CPAAS, row, cause="completed", provider_call_id="pc-1")
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert await _outcome(scratch_pool, row) == "busy"
    assert calls[0][0] == "fake" and calls[0][2] == "pc-1"
    assert (await scratch_pool.fetchval("SELECT provider_call_id FROM campaign_attempts WHERE id = $1", row["id"])) == "pc-1"


async def test_the_stored_provider_call_id_wins_over_the_event_hint(scratch_pool, scratch_redis_raw, monkeypatch):
    calls = _api(monkeypatch, "ended")
    row = await _attempt(scratch_pool, path="cpaas", provider="fake", provider_call_id="stored")
    await _emit(scratch_redis_raw, STREAM_CPAAS, row, provider_call_id="hint")
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert calls[0][2] == "stored" and await _outcome(scratch_pool, row) == "ended"


@pytest.mark.parametrize("to, from_", [("+919999999999", CALLER), (CALLEE, "+910000000000"), (None, None)])
async def test_api_to_or_from_mismatch_on_a_hinted_call_changes_nothing(scratch_pool, scratch_redis_raw, monkeypatch, to, from_):
    _api(monkeypatch, "busy", to=to, from_=from_)
    row = await _attempt(scratch_pool, path="cpaas", provider="fake")
    await _emit(scratch_redis_raw, STREAM_CPAAS, row, provider_call_id="pc-1")
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert await _outcome(scratch_pool, row) is None
    assert await _pending(scratch_redis_raw, STREAM_CPAAS) == 0


@pytest.mark.parametrize("status", ["live", "answered", "unknown", telephony_originate.TelephonyOriginateError("down")])
async def test_non_terminal_or_unavailable_provider_status_is_acked_and_left_to_the_deadline(
    scratch_pool, scratch_redis_raw, monkeypatch, status,
):
    _api(monkeypatch, status)
    row = await _attempt(scratch_pool, path="cpaas", provider="fake")
    await _emit(scratch_redis_raw, STREAM_CPAAS, row, provider_call_id="pc-1")
    assert await outcome_events.drain(scratch_redis_raw, "c1") == 1
    assert await _outcome(scratch_pool, row) is None
    assert await _pending(scratch_redis_raw, STREAM_CPAAS) == 0


async def test_cpaas_answered_event_marks_answered(scratch_pool, scratch_redis_raw):
    row = await _attempt(scratch_pool, path="cpaas", provider="fake")
    await _emit(scratch_redis_raw, STREAM_CPAAS, row, kind="answered", answered=True)
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert await _outcome(scratch_pool, row) == "answered"


async def test_an_unacked_entry_is_redelivered_after_the_idle_window_and_applied_once(scratch_pool, scratch_redis_raw):
    row = await _attempt(scratch_pool)
    entry_id = await _emit(scratch_redis_raw, STREAM_FS, row, cause="USER_BUSY")
    await scratch_redis_raw.xreadgroup(GROUP, "dead-consumer", {STREAM_FS: ">"})  # delivered, never acked
    assert await _pending(scratch_redis_raw, STREAM_FS) == 1

    await scratch_redis_raw.xclaim(STREAM_FS, GROUP, "dead-consumer", 0, [entry_id], idle=30_000)
    assert await outcome_events.drain(scratch_redis_raw, "alive") == 0
    assert await _outcome(scratch_pool, row) is None

    await scratch_redis_raw.xclaim(STREAM_FS, GROUP, "dead-consumer", 0, [entry_id], idle=outcome_events.RECLAIM_IDLE_MS + 1000)
    assert await outcome_events.drain(scratch_redis_raw, "alive") == 1
    assert await _outcome(scratch_pool, row) == "busy"
    assert await _pending(scratch_redis_raw, STREAM_FS) == 0
    assert await outcome_events.drain(scratch_redis_raw, "alive") == 0


async def test_a_foreign_tenants_event_changes_no_row(scratch_pool, scratch_redis_raw):
    row = await _attempt(scratch_pool)
    async with scratch_pool.acquire() as conn:
        other = await seed.tenant(conn)
    await _emit(scratch_redis_raw, STREAM_FS, row, cause="USER_BUSY", tenant_id=other)
    assert await outcome_events.drain(scratch_redis_raw, "c1") == 1
    assert await _outcome(scratch_pool, row) is None
    assert await _pending(scratch_redis_raw, STREAM_FS) == 0


async def test_foreign_tenant_cpaas_hangup_never_reaches_the_provider(scratch_pool, scratch_redis_raw, monkeypatch):
    calls = _api(monkeypatch, "busy")
    row = await _attempt(scratch_pool, path="cpaas", provider="fake")
    async with scratch_pool.acquire() as conn:
        other = await seed.tenant(conn)
    await _emit(scratch_redis_raw, STREAM_CPAAS, row, tenant_id=other, provider_call_id="pc-1")
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert calls == [] and await _outcome(scratch_pool, row) is None


@pytest.mark.parametrize("fields", [
    {"kind": "bogus"}, {"kind": "hangup", "tenant_id": "not-a-uuid"}, {"kind": "hangup", "tenant_id": ""},
])
async def test_malformed_events_are_acked_and_dropped(scratch_pool, scratch_redis_raw, fields):
    row = await _attempt(scratch_pool)
    base = {"source": "fs", "key": row["idempotency_key"], "tenant_id": str(row["tenant_id"]), "cause": "USER_BUSY", "answered": "0"}
    await scratch_redis_raw.xadd(STREAM_FS, {**base, **fields})
    assert await outcome_events.drain(scratch_redis_raw, "c1") == 1
    assert await _outcome(scratch_pool, row) is None and await _pending(scratch_redis_raw, STREAM_FS) == 0


async def test_conv_entries_are_handled_before_the_other_streams(scratch_pool, scratch_redis_raw, monkeypatch):
    seen: list[str] = []

    async def record(stream, fields):
        seen.append(stream)

    monkeypatch.setattr(outcome_events, "_handle", record)
    row = await _attempt(scratch_pool)
    await _emit(scratch_redis_raw, STREAM_FS, row)
    await _emit(scratch_redis_raw, STREAM_CPAAS, row)
    await _emit(scratch_redis_raw, STREAM_CONV, row, kind="dnc_request", source="conv")
    await outcome_events.drain(scratch_redis_raw, "c1")
    assert seen[0] == STREAM_CONV and sorted(seen) == sorted([STREAM_FS, STREAM_CPAAS, STREAM_CONV])


async def test_ensure_groups_is_idempotent(scratch_redis_raw):
    await outcome_events.ensure_groups(scratch_redis_raw)
    await outcome_events.ensure_groups(scratch_redis_raw)
