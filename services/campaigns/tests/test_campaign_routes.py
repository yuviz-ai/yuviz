"""Readiness gating, the attempts and readiness routes, and progress wait fields, through the real router."""

from __future__ import annotations

import logging
import typing
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import Header

from libs.tenancy import set_caller_tenant
from services.campaigns import app as app_module, attempts, capacity, campaigns, readiness, telephony_originate
from services.campaigns.billing import CostEstimate, NullBillingAdmission
from services.campaigns.tests import seed, world as w
from services.config.auth import CurrentUser
from services.config.deps import get_current_user


@pytest.fixture
async def api(scratch_pool):
    user_id = str(await scratch_pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", f"{uuid.uuid4()}@example.test",
    ))

    async def fake_user(x_role: str = Header(), x_tenant: str | None = Header(default=None)):
        set_caller_tenant(x_tenant)
        return CurrentUser(id=user_id, email="t@example.test", role=x_role, tenant_id=x_tenant)

    app = app_module.app
    app.dependency_overrides[get_current_user] = fake_user
    dial = w.FakeDial()
    app.state.dial_client, app.state.billing = dial, NullBillingAdmission()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.dial = dial
        yield client
    app.dependency_overrides.clear()


def _as(world, role="admin"):
    return {"x-role": role, "x-tenant": str(world["tenant_id"])}


async def _world(pool, **kwargs):
    async with pool.acquire() as conn:
        world = await w.make(conn, status="draft", **kwargs)
        await seed.contact(conn, world["campaign"]["id"])
    return world


async def test_a_campaign_missing_its_caller_id_returns_409_checks_and_stays_draft(scratch_pool, api):
    world = await _world(scratch_pool)
    await scratch_pool.execute("UPDATE campaigns SET caller_id = NULL WHERE id = $1", world["campaign"]["id"])
    resp = await api.post(f"/campaigns/{world['campaign']['id']}/start", headers=_as(world))
    assert resp.status_code == 409
    assert "caller_id_missing" in [c["id"] for c in resp.json()["checks"]]
    assert (await w.refreshed(scratch_pool, world["campaign"]["id"]))["status"] == "draft"


async def test_a_ready_campaign_starts_and_resume_is_gated_the_same_way(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    api.dial.probe_ok = True
    await scratch_pool.execute("UPDATE phone_numbers SET agent_id = $2 WHERE tenant_id = $1", world["tenant_id"], world["campaign"]["agent_id"])
    resp = await api.post(f"/campaigns/{cid}/start", headers=_as(world))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"

    await api.post(f"/campaigns/{cid}/pause", headers=_as(world))
    api.dial.probe_ok = False
    resp = await api.post(f"/campaigns/{cid}/resume", headers=_as(world))
    assert resp.status_code == 409
    assert "no_healthy_sip_node" in [c["id"] for c in resp.json()["checks"]]
    assert (await w.refreshed(scratch_pool, cid))["status"] == "paused"


async def test_readiness_route_reports_the_checks(scratch_pool, api):
    world = await _world(scratch_pool)
    api.dial.probe_ok = False
    resp = await api.get(f"/campaigns/{world['campaign']['id']}/readiness", headers=_as(world, "viewer"))
    body = resp.json()
    assert resp.status_code == 200 and body["ready"] is False
    assert "no_healthy_sip_node" in [c["id"] for c in body["checks"]]


async def test_attempt_rows_never_contain_the_callee(scratch_pool, api):
    world = await _world(scratch_pool)
    async with scratch_pool.acquire() as conn:
        _, att = await w.open_attempt(conn, world)
    resp = await api.get(f"/campaigns/{world['campaign']['id']}/attempts", headers=_as(world, "viewer"))
    rows = resp.json()
    assert resp.status_code == 200 and len(rows) == 1
    assert "callee" not in rows[0] and att["callee"] not in resp.text


@pytest.mark.parametrize("reason", typing.get_args(capacity.WaitReason))
async def test_progress_reports_each_wait_reason_with_its_waiting_count(scratch_pool, api, reason):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    async with scratch_pool.acquire() as conn:
        await seed.contact(conn, cid)
        await capacity.write_wait_reason(conn, cid, reason)
    body = (await api.get(f"/campaigns/{cid}/progress", headers=_as(world, "viewer"))).json()
    assert (body["wait_reason"], body["waiting_count"], body["pending"]) == (reason, 2, 2)
    assert body["wait_since"] is not None and body["effective_limit"] == world["campaign"]["max_concurrent_calls"]


async def test_progress_without_a_wait_reason_has_no_waiting_count(scratch_pool, api):
    world = await _world(scratch_pool)
    body = (await api.get(f"/campaigns/{world['campaign']['id']}/progress", headers=_as(world, "viewer"))).json()
    assert (body["wait_reason"], body["waiting_count"]) == (None, 0)


async def test_effective_limit_is_the_lower_of_campaign_and_tenant_caps(scratch_pool):
    async with scratch_pool.acquire() as conn:
        world = await w.make(conn, max_concurrent_calls=5)
        await conn.execute("UPDATE tenants SET max_outbound_concurrent_calls = 2 WHERE id = $1", world["tenant_id"])
    progress = await campaigns.get_progress(world["campaign"]["id"], platform_scoped=True)
    assert (progress["tenant_cap"], progress["effective_limit"]) == (2, 2)


_BLOCK_IDS = [
    "agent_unavailable", "caller_id_missing", "caller_id_not_owned",
    "no_pending_contacts", "timezone_invalid", "provider_not_ready", "provider_not_campaign_capable",
    "no_healthy_sip_node",
]


async def _break(pool, api, world, check_id, monkeypatch):
    cid, agent_id = world["campaign"]["id"], world["campaign"]["agent_id"]
    if check_id == "agent_unavailable":
        await pool.execute("UPDATE agents SET deleted_at = now() WHERE id = $1", agent_id)
    elif check_id == "caller_id_missing":
        await pool.execute("UPDATE campaigns SET caller_id = NULL WHERE id = $1", cid)
    elif check_id == "caller_id_not_owned":
        await pool.execute("UPDATE campaigns SET caller_id = '+14155550999' WHERE id = $1", cid)
    elif check_id == "no_pending_contacts":
        await pool.execute("UPDATE campaign_contacts SET status = 'completed' WHERE campaign_id = $1", cid)
    elif check_id == "timezone_invalid":
        await pool.execute("UPDATE campaigns SET calling_hours_timezone = 'Mars/Base' WHERE id = $1", cid)
    elif check_id == "provider_not_ready":
        async def not_ready(*, provider, tenant_slug):
            return {"ready": False, "reason": "provider_unhealthy"}

        monkeypatch.setattr(telephony_originate, "outbound_readiness", not_ready)
    elif check_id == "no_healthy_sip_node":
        api.dial.probe_ok = False


@pytest.mark.parametrize("check_id", _BLOCK_IDS)
@pytest.mark.parametrize("action", ["start", "resume"])
async def test_every_block_gives_409_and_leaves_the_status_alone(scratch_pool, api, monkeypatch, check_id, action):
    provider = {"provider_not_ready": "fake", "provider_not_campaign_capable": "cloudonix"}.get(check_id)
    world = await _world(scratch_pool, provider=provider)
    cid = world["campaign"]["id"]
    status = "draft" if action == "start" else "paused"
    await scratch_pool.execute("UPDATE campaigns SET status = $2 WHERE id = $1", cid, status)
    await _break(scratch_pool, api, world, check_id, monkeypatch)
    resp = await api.post(f"/campaigns/{cid}/{action}", headers=_as(world))
    assert resp.status_code == 409, resp.text
    assert {"id": check_id, "level": "block"}.items() <= next(c for c in resp.json()["checks"] if c["id"] == check_id).items()
    assert (await w.refreshed(scratch_pool, cid))["status"] == status


async def test_a_lone_campaign_cap_warning_still_starts(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    await scratch_pool.execute("UPDATE tenants SET max_outbound_concurrent_calls = 1 WHERE id = $1", world["tenant_id"])
    await scratch_pool.execute("UPDATE campaigns SET max_concurrent_calls = 5 WHERE id = $1", cid)
    readiness_body = (await api.get(f"/campaigns/{cid}/readiness", headers=_as(world))).json()
    assert [c["id"] for c in readiness_body["checks"]] == ["campaign_cap_above_tenant_cap"]
    resp = await api.post(f"/campaigns/{cid}/start", headers=_as(world))
    assert resp.status_code == 200 and resp.json()["status"] == "running"


async def test_the_readiness_route_renders_a_stubbed_estimate(scratch_pool, api, monkeypatch):
    async def stub(conn, billing, campaign):
        return CostEstimate(
            currency="USD", calls=1, avg_billable_s=60, avg_source="default", per_call_minor=12, total_minor=12,
        )

    monkeypatch.setattr(readiness, "estimate_campaign", stub)
    world = await _world(scratch_pool)
    body = (await api.get(f"/campaigns/{world['campaign']['id']}/readiness", headers=_as(world))).json()
    assert (body["estimate"]["currency"], body["estimate"]["per_call_minor"], body["estimate"]["total_minor"]) == ("USD", 12, 12)


async def test_attempts_carry_a_masked_callee_and_a_disposition_only_for_an_answered_own_call(
    scratch_pool, api, caplog,
):
    world = await _world(scratch_pool)
    camp = world["campaign"]
    async with scratch_pool.acquire() as conn:
        other_tenant = await seed.tenant(conn)
        other_slug = await conn.fetchval("SELECT slug FROM tenants WHERE id = $1", other_tenant)
        slug = await conn.fetchval("SELECT slug FROM tenants WHERE id = $1", world["tenant_id"])
        rows = {}
        for name in ("answered", "no_answer", "foreign"):
            _, att = await w.open_attempt(conn, world)
            rows[name] = att
            session = att["call_session_id"]
            call_slug = other_slug if name == "foreign" else slug
            await conn.execute(
                "INSERT INTO calls (session_id, tenant_id, disposition) VALUES ($1, $2, 'completed')", session, call_slug,
            )
        await conn.execute("UPDATE campaign_attempts SET answered_at = now() WHERE id = $1", rows["answered"]["id"])
        await conn.execute("UPDATE campaign_attempts SET answered_at = now() WHERE id = $1", rows["foreign"]["id"])
    with caplog.at_level(logging.DEBUG):
        resp = await api.get(f"/campaigns/{camp['id']}/attempts", headers=_as(world, "viewer"))
    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()}
    assert by_id[str(rows["answered"]["id"])]["disposition"] == "completed"
    assert by_id[str(rows["no_answer"]["id"])]["disposition"] is None
    assert by_id[str(rows["foreign"]["id"])]["disposition"] is None
    for att in rows.values():
        digits = "".join(ch for ch in att["callee"] if ch.isdigit())
        masked = by_id[str(att["id"])]["callee_masked"]
        assert masked.endswith(digits[-4:]) and masked != att["callee"]
        assert att["callee"] not in resp.text and digits not in resp.text and digits not in caplog.text
        audit_text = " ".join(str(r["new_value"]) for r in await scratch_pool.fetch("SELECT new_value FROM audit_log"))
        assert digits not in audit_text


@pytest.mark.parametrize("value, expected", [(None, None), ("+14155550123", "•••0123"), ("5003", "•••"), ("", "•••")])
def test_mask_callee(value, expected):
    assert attempts._mask_callee(value) == expected


@pytest.mark.parametrize("field", ["max_attempts", "retry_gap_minutes"])
async def test_a_zero_attempt_or_gap_is_a_400_on_patch_and_create(scratch_pool, api, field):
    world = await _world(scratch_pool)
    patched = await api.patch(f"/campaigns/{world['campaign']['id']}", json={field: 0}, headers=_as(world))
    assert patched.status_code == 400, patched.text
    created = await api.post(
        f"/tenants/{world['tenant_id']}/campaigns",
        json={"agent_id": str(world["campaign"]["agent_id"]), "name": "n", field: 0}, headers=_as(world),
    )
    assert created.status_code == 400, created.text


async def test_an_identical_value_patch_writes_one_audit_row_and_changes_only_updated_at(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    before = await w.refreshed(scratch_pool, cid)
    resp = await api.patch(f"/campaigns/{cid}", json={"name": before["name"]}, headers=_as(world))
    assert resp.status_code == 200, resp.text
    after = await w.refreshed(scratch_pool, cid)
    assert {k for k in before if before[k] != after[k]} == {"updated_at"}
    assert await scratch_pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE entity_type = 'campaign' AND entity_id = $1", cid,
    ) == 1


@pytest.mark.parametrize("method, path, body", [
    ("post", "start", None), ("post", "pause", None), ("post", "resume", None), ("patch", "", {"name": "x"}),
])
async def test_a_viewer_cannot_start_pause_resume_or_patch(scratch_pool, api, method, path, body):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    resp = await getattr(api, method)(f"/campaigns/{cid}/{path}".rstrip("/"), headers=_as(world, "viewer"), **({"json": body} if body else {}))
    assert resp.status_code == 403
    assert (await w.refreshed(scratch_pool, cid))["status"] == "draft"


async def test_progress_reports_the_campaign_status(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    assert (await api.get(f"/campaigns/{cid}/progress", headers=_as(world, "viewer"))).json()["status"] == "draft"
    await scratch_pool.execute("UPDATE campaigns SET status = 'paused' WHERE id = $1", cid)
    assert (await api.get(f"/campaigns/{cid}/progress", headers=_as(world, "viewer"))).json()["status"] == "paused"


async def test_caller_ids_lists_own_dids_with_their_path_badge(scratch_pool, api):
    world = await _world(scratch_pool, provider="fake")
    native_did = f"1{uuid.uuid4().int % 10**10:010d}"
    async with scratch_pool.acquire() as conn:
        await conn.execute("INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)", native_did, world["tenant_id"])
        other = await seed.tenant(conn)
        await conn.execute("INSERT INTO phone_numbers (did, tenant_id) VALUES ('19990000001', $1)", other)
    resp = await api.get(f"/tenants/{world['tenant_id']}/campaigns/caller-ids", headers=_as(world, "viewer"))
    assert resp.status_code == 200, resp.text
    paths = {row["did"]: row["path"] for row in resp.json()}
    assert paths[native_did] == "native" and set(paths.values()) == {"native", "fake"}
    assert "19990000001" not in paths


# ── create: caller ID comes from the agent, optional scheduled start ─────

async def _agent_dials_as(pool, world, caller_id, direction="outbound"):
    await pool.execute(
        "UPDATE agents SET outbound_caller_id = $2, direction = $3 WHERE id = $1",
        world["campaign"]["agent_id"], caller_id, direction,
    )


async def _create(api, world, **body):
    return await api.post(
        f"/tenants/{world['tenant_id']}/campaigns",
        json={"agent_id": str(world["campaign"]["agent_id"]), "name": "n", **body}, headers=_as(world),
    )


async def test_create_takes_the_caller_id_from_the_agent_and_ignores_one_in_the_body(scratch_pool, api):
    world = await _world(scratch_pool)
    await _agent_dials_as(scratch_pool, world, world["campaign"]["caller_id"])
    resp = await _create(api, world, caller_id="+19999999999")
    assert resp.status_code == 201, resp.text
    assert (resp.json()["caller_id"], resp.json()["status"]) == (world["campaign"]["caller_id"], "draft")


@pytest.mark.parametrize("caller_id, direction, detail", [
    (None, "outbound", "no outbound caller ID"),
    ("owned", "inbound", "inbound-only"),
    ("+14155550999", "outbound", "no longer owned"),
])
async def test_create_refuses_an_agent_that_cannot_dial(scratch_pool, api, caller_id, direction, detail):
    world = await _world(scratch_pool)
    await _agent_dials_as(
        scratch_pool, world, world["campaign"]["caller_id"] if caller_id == "owned" else caller_id, direction,
    )
    resp = await _create(api, world)
    assert resp.status_code == 422, resp.text
    assert detail in resp.json()["detail"]


async def test_a_scheduled_campaign_is_created_scheduled_and_must_be_a_future_aware_time(scratch_pool, api):
    world = await _world(scratch_pool)
    await _agent_dials_as(scratch_pool, world, world["campaign"]["caller_id"])
    now = datetime.now(timezone.utc)

    ok = await _create(api, world, scheduled_start_at=(now + timedelta(hours=1)).isoformat())
    assert ok.status_code == 201, ok.text
    assert ok.json()["status"] == "scheduled"

    past = await _create(api, world, scheduled_start_at=(now - timedelta(minutes=1)).isoformat())
    naive = await _create(api, world, scheduled_start_at="2099-01-01T10:00:00")
    assert (past.status_code, naive.status_code) == (422, 422)


async def test_start_picks_up_the_agents_current_caller_id(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    owned = world["campaign"]["caller_id"]
    await scratch_pool.execute("UPDATE campaigns SET caller_id = NULL WHERE id = $1", cid)
    await _agent_dials_as(scratch_pool, world, owned)
    resp = await api.post(f"/campaigns/{cid}/start", headers=_as(world))
    assert resp.status_code == 200, resp.text
    assert (resp.json()["status"], resp.json()["caller_id"]) == ("running", owned)


async def test_a_description_is_saved_on_create_and_can_be_edited(scratch_pool, api):
    world = await _world(scratch_pool)
    await _agent_dials_as(scratch_pool, world, world["campaign"]["caller_id"])
    created = await _create(api, world, description="Remind patients of tomorrow's appointments")
    assert created.status_code == 201, created.text
    assert created.json()["description"] == "Remind patients of tomorrow's appointments"
    edited = await api.patch(f"/campaigns/{created.json()['id']}", json={"description": "Updated"}, headers=_as(world))
    assert edited.json()["description"] == "Updated"
    too_long = await _create(api, world, description="x" * 1001)
    assert too_long.status_code == 400


async def test_a_cancelled_campaign_cannot_be_started_or_resumed_again(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    await scratch_pool.execute("UPDATE campaigns SET status = 'running' WHERE id = $1", cid)
    cancelled = await api.post(f"/campaigns/{cid}/cancel", headers=_as(world))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert (await api.post(f"/campaigns/{cid}/start", headers=_as(world))).status_code == 409
    assert (await api.post(f"/campaigns/{cid}/resume", headers=_as(world))).status_code == 409


async def test_a_finished_campaign_cannot_be_cancelled(scratch_pool, api):
    world = await _world(scratch_pool)
    await scratch_pool.execute("UPDATE campaigns SET status = 'completed' WHERE id = $1", world["campaign"]["id"])
    resp = await api.post(f"/campaigns/{world['campaign']['id']}/cancel", headers=_as(world))
    assert resp.status_code == 409


async def test_only_a_draft_can_be_deleted_and_it_leaves_the_list(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    await scratch_pool.execute("UPDATE campaigns SET status = 'running' WHERE id = $1", cid)
    assert (await api.delete(f"/campaigns/{cid}", headers=_as(world))).status_code == 409
    await scratch_pool.execute("UPDATE campaigns SET status = 'draft' WHERE id = $1", cid)
    assert (await api.delete(f"/campaigns/{cid}", headers=_as(world))).status_code == 204
    listed = await api.get(f"/tenants/{world['tenant_id']}/campaigns", headers=_as(world))
    assert all(c["id"] != str(cid) for c in listed.json())


async def test_call_again_makes_a_draft_with_only_the_chosen_unreached_leads(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    await scratch_pool.execute("UPDATE campaigns SET status = 'completed' WHERE id = $1", cid)
    async with scratch_pool.acquire() as conn:
        missed = await seed.contact(conn, cid, status="no_answer", phone="+14155550201")
        answered = await seed.contact(conn, cid, status="completed", phone="+14155550202")
    resp = await api.post(
        f"/campaigns/{cid}/recall", json={"contact_ids": [str(missed["id"]), str(answered["id"])]}, headers=_as(world),
    )
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert (new["status"], new["caller_id"], new["agent_id"]) == (
        "draft", world["campaign"]["caller_id"], str(world["campaign"]["agent_id"]),
    )
    contacts = await scratch_pool.fetch("SELECT phone_number FROM campaign_contacts WHERE campaign_id = $1", new["id"])
    assert [r["phone_number"] for r in contacts] == ["+14155550201"]


async def test_the_call_log_lists_campaign_calls_and_filters_by_status(scratch_pool, api):
    from services.campaigns import attempts as attempts_service

    world = await _world(scratch_pool)
    async with scratch_pool.acquire() as conn:
        campaign = dict(await conn.fetchrow("SELECT * FROM campaigns WHERE id = $1", world["campaign"]["id"]))
        for outcome in ("no_answer", "busy"):
            contact = await seed.contact(conn, world["campaign"]["id"], phone=f"+1415555{len(outcome):04d}")
            row = await attempts_service.create_attempt(
                conn, campaign=campaign, contact=dict(contact), path="sip", provider=None, cps_scope="sip_trunk:pstn",
            )
            await conn.execute(
                "UPDATE campaign_attempts SET state = 'resolved', outcome = $2, resolved_at = now() WHERE id = $1",
                row["id"], outcome,
            )
    base = f"/tenants/{world['tenant_id']}/campaigns/call-attempts"
    everything = (await api.get(base, headers=_as(world))).json()
    assert {a["outcome"] for a in everything} >= {"no_answer", "busy"}
    assert all("•" in a["callee_masked"] for a in everything)
    busy = (await api.get(base, params={"status": "busy"}, headers=_as(world))).json()
    assert [a["outcome"] for a in busy] == ["busy"]
    assert all(isinstance(a["can_call_again"], bool) for a in everything)
    inbound = (await api.get(base, params={"direction": "inbound"}, headers=_as(world))).json()
    assert inbound == []


async def test_cancelling_closes_attempts_that_were_never_sent(scratch_pool, api):
    world = await _world(scratch_pool)
    cid = world["campaign"]["id"]
    await scratch_pool.execute("UPDATE campaigns SET status = 'running' WHERE id = $1", cid)
    async with scratch_pool.acquire() as conn:
        from services.campaigns import attempts as attempts_service
        campaign = dict(await conn.fetchrow("SELECT * FROM campaigns WHERE id = $1", cid))
        contact = await conn.fetchrow("SELECT * FROM campaign_contacts WHERE campaign_id = $1 LIMIT 1", cid)
        row = await attempts_service.create_attempt(
            conn, campaign=campaign, contact=dict(contact), path="sip", provider=None, cps_scope="sip_trunk:pstn",
        )
    assert (await api.post(f"/campaigns/{cid}/cancel", headers=_as(world))).status_code == 200
    state = await scratch_pool.fetchrow("SELECT state, outcome, failure_reason FROM campaign_attempts WHERE id = $1", row["id"])
    assert (state["state"], state["outcome"], state["failure_reason"]) == ("resolved", "failed", "campaign_cancelled")
