"""
Live Calls Monitoring — T2 (fresh_authority/assert_current_authority), T4
(get_live_calls query shape), T5b (the permanent stale-token-read tripwire),
T6 (tenant isolation / no existence oracle / platform scope / re-tenanted
superadmin), T7 (AC15 snippet authority), T8 (KPI split), T9 (rate limit +
acquire timeout), T10-T12 (POST /interventions: type-safe scope predicate,
ip_address sourcing, denial auditing), T13 (AC9 stale-token + audit
completeness for interventions), T14 (denial-audit aggregation), T15
(bounded/selective fresh_authority memo).

Real Postgres + Redis, same convention as test_calls.py/test_console_gate.py
— fixtures test_tenant/test_admin/test_viewer/pool from conftest.py, plus
local helpers below for rows this feature reads/writes that conftest has no
fixture for (calls, agents, transcript_entries, live_call_interventions,
and users at roles/tenant-shapes conftest doesn't already mint).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import uuid

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from services.config import audit
from services.config import auth, cache, db, deps
from services.config import live_calls
from services.config import tenants as tenants_service
from services.config import users as users_service
from services.config.app import app
from services.config.routers import live_calls as live_calls_router


# ── helpers ──────────────────────────────────────────────────────────────

def _client_as(user: dict) -> AsyncClient:
    token = auth.create_access_token(user)
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"},
    )


async def _create_user(*, role: str, tenant_id=None, email: str | None = None) -> dict:
    email = email or f"test-livecalls-{uuid.uuid4().hex[:8]}@example.com"
    return await users_service.create_user(
        email=email, password="test-password-not-real", role=role, tenant_id=tenant_id,
    )


async def _create_service_account_viewer(pool) -> dict:
    # scripts/create_service_account.py's exact shape: viewer role, NULL
    # tenant, is_service_account=true.
    email = f"test-svc-{uuid.uuid4().hex[:8]}@internal.yuviz.ai"
    row = await pool.fetchrow(
        "INSERT INTO users (email, password_hash, role, tenant_id, is_service_account) "
        "VALUES ($1, 'x', 'viewer', NULL, true) RETURNING *",
        email,
    )
    return dict(row)


async def _soft_delete_user(pool, user_id) -> None:
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user_id)


async def _hard_delete_user(pool, user_id) -> None:
    await pool.execute("DELETE FROM users WHERE id = $1", user_id)


async def _insert_call(
    pool, *, tenant_slug: str, session_id: str | None = None, direction: str = "inbound",
    live_stage: str | None = None, agent_id=None,
    caller_number: str = "+14155557788", called_number: str = "+14155550100", ended: bool = False,
) -> str:
    session_id = session_id or f"test-live-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction, caller_number, called_number, "
        "agent_id, live_stage, ended_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        session_id, tenant_slug, direction, caller_number, called_number, agent_id, live_stage,
        None if not ended else "now()",
    )
    return session_id


async def _insert_agent(pool, *, tenant_id, name: str = "Reception"):
    row = await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, $2, $3) RETURNING id",
        tenant_id, f"test-agent-{uuid.uuid4().hex[:8]}", name,
    )
    return row["id"]


async def _insert_transcript_turn(pool, *, session_id: str, turn_number: int = 1, caller_text=None, ai_response=None):
    await pool.execute(
        "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
        "VALUES ($1, $2, $3, $4)",
        session_id, turn_number, caller_text, ai_response,
    )


async def _cleanup_call(pool, session_id: str) -> None:
    await pool.execute("DELETE FROM live_call_interventions WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)


async def _cleanup_agent(pool, agent_id) -> None:
    await pool.execute("DELETE FROM agents WHERE id = $1", agent_id)


# ── T2 — fresh_authority / assert_current_authority ─────────────────────

class TestFreshAuthority:
    async def test_memoizes_within_ttl_then_rereads_after_role_change_and_expiry(
        self, pool, test_tenant, monkeypatch,
    ):
        user_row = await _create_user(role="supervisor", tenant_id=test_tenant["id"])
        try:
            token_user = auth.decode_access_token(auth.create_access_token(user_row))

            class _AppState:
                pass

            app_state = _AppState()

            calls = {"count": 0}
            original = users_service.get_user_by_id

            async def _counting_get_user_by_id(user_id):
                calls["count"] += 1
                return await original(user_id)

            # deps.py does `from . import users as users_service`, so
            # deps.users_service IS the services.config.users module — patch
            # it there so fresh_authority's own call is counted.
            monkeypatch.setattr(deps.users_service, "get_user_by_id", _counting_get_user_by_id)

            first = await deps.fresh_authority(app_state, token_user, "self", ttl_s=60)
            second = await deps.fresh_authority(app_state, token_user, "self", ttl_s=60)
            assert calls["count"] == 1
            assert first.role == second.role == "supervisor"

            # Role changes in the DB; within the TTL the memo still wins.
            await users_service.update_user(user_row["id"], role="admin")
            still_memoized = await deps.fresh_authority(app_state, token_user, "self", ttl_s=60)
            assert still_memoized.role == "supervisor"
            assert calls["count"] == 1

            # Force expiry with a ttl_s of 0 rather than sleeping — same
            # code path, deterministic.
            third = await deps.fresh_authority(app_state, token_user, "self", ttl_s=0)
            assert third.role == "admin"
            assert calls["count"] == 2
        finally:
            await _soft_delete_user(pool, user_row["id"])

    async def test_assert_current_authority_403s_on_soft_delete(self, pool, test_tenant):
        user_row = await _create_user(role="admin", tenant_id=test_tenant["id"])
        token_user = auth.decode_access_token(auth.create_access_token(user_row))
        await _soft_delete_user(pool, user_row["id"])
        with pytest.raises(Exception) as exc_info:
            await deps.assert_current_authority(token_user)
        assert exc_info.value.status_code == 403

    async def test_assert_current_authority_403s_on_tenant_mismatch(self, pool, test_tenant):
        user_row = await _create_user(role="admin", tenant_id=test_tenant["id"])
        try:
            token_user = auth.decode_access_token(auth.create_access_token(user_row))
            other_tenant = await pool.fetchrow(
                "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
                "Other", f"test-other-{uuid.uuid4().hex[:8]}",
            )
            try:
                await users_service.update_user(user_row["id"], tenant_id=other_tenant["id"])
                with pytest.raises(Exception) as exc_info:
                    await deps.assert_current_authority(token_user)
                assert exc_info.value.status_code == 403
            finally:
                await pool.execute("UPDATE users SET tenant_id = NULL WHERE id = $1", user_row["id"])
                await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])
        finally:
            await _soft_delete_user(pool, user_row["id"])


# ── T4 — get_live_calls query shape ──────────────────────────────────────

class TestGetLiveCallsQuery:
    async def test_withheld_transcript_is_never_fetched(self, pool, test_tenant, monkeypatch):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        await _insert_transcript_turn(pool, session_id=session_id, caller_text="secret caller text")
        try:
            captured: dict[str, str] = {}
            original_fetch = asyncpg.Connection.fetch

            async def _spy_fetch(self, query, *args, **kwargs):
                captured["sql"] = query
                return await original_fetch(self, query, *args, **kwargs)

            monkeypatch.setattr(asyncpg.Connection, "fetch", _spy_fetch)

            result = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=False)
            item = result["items"][0]
            assert item["transcript_snippet"] is None
            assert item["transcript_withheld"] is True
            assert "transcript_entries" not in captured["sql"]
        finally:
            await _cleanup_call(pool, session_id)

    async def test_included_transcript_is_fetched(self, pool, test_tenant):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        await _insert_transcript_turn(pool, session_id=session_id, caller_text="hello there")
        try:
            result = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=True)
            item = result["items"][0]
            assert item["transcript_snippet"] == "hello there"
            assert item["transcript_withheld"] is False
        finally:
            await _cleanup_call(pool, session_id)

    async def test_cross_tenant_agent_id_never_leaks_agent_name(self, pool, test_tenant):
        other_tenant = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Tenant", f"test-other-{uuid.uuid4().hex[:8]}",
        )
        foreign_agent_id = await _insert_agent(pool, tenant_id=other_tenant["id"], name="Foreign Agent")
        # A call in test_tenant whose agent_id points at an agent that
        # belongs to a DIFFERENT tenant (the collision finding #9 closes) —
        # without the explicit `AND a.tenant_id = $1` predicate, the LEFT
        # JOIN would resolve to and leak the foreign tenant's agent name.
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"], agent_id=foreign_agent_id)
        try:
            result = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=False)
            item = result["items"][0]
            assert item["agent_name"] is None
            assert "Foreign Agent" not in str(result)
        finally:
            await _cleanup_call(pool, session_id)
            await _cleanup_agent(pool, foreign_agent_id)
            await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


# ── T5b — the permanent stale-token-read tripwire ────────────────────────

def _fresh_authority_call_line(tree: ast.AST) -> int:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "fresh_authority":
                return node.lineno
    raise AssertionError("no fresh_authority( call found in routers/live_calls.py")


def _stale_token_reads_after(tree: ast.AST, after_line: int) -> list[str]:
    violations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ("tenant_id", "role")
            and isinstance(node.value, ast.Name)
            and node.value.id == "user"
            and node.lineno > after_line
        ):
            violations.append(f"line {node.lineno}: user.{node.attr}")
    return violations


def test_no_stale_token_read_after_fresh_authority():
    """Structural enforcement of the design's central invariant: after
    fresh_authority() is called, `user` (the token) must never be read for
    `.tenant_id`/`.role` again anywhere in this file — see T5's
    _resolve_scope docstring. AST-based so it survives reformatting."""
    source = inspect.getsource(live_calls_router)
    tree = ast.parse(source)
    fresh_authority_line = _fresh_authority_call_line(tree)
    violations = _stale_token_reads_after(tree, fresh_authority_line)
    assert violations == [], violations


def test_no_stale_token_read_invariant_actually_catches_a_regression():
    """Mutation proof: a temporarily-inserted stale read after the
    fresh_authority( call must fail the invariant above."""
    source = inspect.getsource(live_calls_router)
    # AST-based line lookup, not a text search — the module docstring also
    # mentions "fresh_authority()", which a naive `"fresh_authority(" in
    # line` search would match first and mutate the wrong (non-code) line.
    real_call_line = _fresh_authority_call_line(ast.parse(source))
    lines = source.splitlines()
    mutated_lines = lines[:real_call_line] + ["    _ = user.tenant_id"] + lines[real_call_line:]
    mutated_source = "\n".join(mutated_lines)

    tree = ast.parse(mutated_source)
    fresh_authority_line = _fresh_authority_call_line(tree)
    violations = _stale_token_reads_after(tree, fresh_authority_line)
    assert violations != [], "mutation was not detected — the invariant test is not load-bearing"


# ── T6 — tenant isolation, no existence oracle, platform scope, re-tenanting ─

async def _create_tenant(pool, *, name: str = "Other Tenant") -> dict:
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
        name, f"test-{uuid.uuid4().hex[:8]}",
    )
    return dict(row)


async def _cleanup_tenant(pool, tenant: dict) -> None:
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


def _clear_authority_memo() -> None:
    # Deleting the attribute (not assigning `{}`) so fresh_authority()
    # recreates it as the OrderedDict it expects (T15's bounded LRU memo).
    if hasattr(app.state, "_live_calls_authority_memo"):
        del app.state._live_calls_authority_memo


def _reset_throttle() -> None:
    # T9's per-user token bucket is sized to the real 5s poll interval, so
    # tests that deliberately poll the same user faster than that (to
    # exercise fresh_authority/_resolve_scope, not the throttle itself) must
    # reset it between requests — see TestRateLimitAndAcquireTimeout below
    # for the throttle's own dedicated test.
    app.state.live_calls_throttle._counter._buckets.clear()


class TestTenantIsolationAndScope:
    async def test_tenant_scoped_admin_sees_only_own_tenant(self, pool, test_tenant, test_admin):
        other_tenant = await _create_tenant(pool)
        call_a = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        call_b = await _insert_call(pool, tenant_slug=other_tenant["slug"])
        try:
            async with _client_as(test_admin["user"]) as client:
                resp = await client.get("/live-calls")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["tenant_slug"] == test_tenant["slug"]
            assert [item["session_id"] for item in body["items"]] == [call_a]
            assert body["kpis"]["live_calls"] == 1
        finally:
            await _cleanup_call(pool, call_a)
            await _cleanup_call(pool, call_b)
            await _cleanup_tenant(pool, other_tenant)

    async def test_no_existence_oracle_for_foreign_vs_nonexistent_tenant_slug(
        self, pool, test_tenant, test_admin,
    ):
        other_tenant = await _create_tenant(pool)
        try:
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                foreign_resp = await client.get("/live-calls", params={"tenant_slug": other_tenant["slug"]})
                _reset_throttle()
                nonexistent_resp = await client.get(
                    "/live-calls", params={"tenant_slug": f"nope-{uuid.uuid4().hex[:8]}"},
                )
            assert foreign_resp.status_code == nonexistent_resp.status_code == 404
            assert foreign_resp.json() == nonexistent_resp.json() == {"detail": "tenant not found"}
        finally:
            await _cleanup_tenant(pool, other_tenant)

    async def test_superadmin_nonexistent_slug_matches_the_same_404(self, pool, test_tenant):
        superadmin = await _create_user(role="superadmin", tenant_id=None)
        try:
            async with _client_as(superadmin) as client:
                resp = await client.get(
                    "/live-calls", params={"tenant_slug": f"nope-{uuid.uuid4().hex[:8]}"},
                )
            assert resp.status_code == 404
            assert resp.json() == {"detail": "tenant not found"}
        finally:
            await _soft_delete_user(pool, superadmin["id"])

    async def test_null_tenant_viewer_service_account_403s(self, pool, test_tenant):
        service_account = await _create_service_account_viewer(pool)
        try:
            async with _client_as(service_account) as client:
                resp = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
            assert resp.status_code == 403
        finally:
            await _hard_delete_user(pool, service_account["id"])

    async def test_null_tenant_superadmin_requires_slug_then_scopes_to_it(self, pool, test_tenant):
        superadmin = await _create_user(role="superadmin", tenant_id=None)
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(superadmin) as client:
                no_slug_resp = await client.get("/live-calls")
                assert no_slug_resp.status_code == 400
                assert no_slug_resp.json() == {"detail": "tenant_slug is required"}

                _reset_throttle()
                with_slug_resp = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
                assert with_slug_resp.status_code == 200
                assert with_slug_resp.json()["tenant_slug"] == test_tenant["slug"]
        finally:
            await _soft_delete_user(pool, superadmin["id"])

    async def test_selection_time_revalidation_after_soft_delete(self, pool, test_tenant):
        other_tenant = await _create_tenant(pool)
        superadmin = await _create_user(role="superadmin", tenant_id=None)
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(superadmin) as client:
                first = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
                assert first.status_code == 200

                await _soft_delete_user(pool, superadmin["id"])

                # Same (already-validated) scope_key, still inside the 60s
                # memo — the documented, bounded window.
                _reset_throttle()
                still_ok = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
                assert still_ok.status_code == 200

                # A DIFFERENT scope_key (tenant B) has never been validated,
                # so it re-reads immediately and 403s.
                _reset_throttle()
                other = await client.get("/live-calls", params={"tenant_slug": other_tenant["slug"]})
                assert other.status_code == 403

                # Clearing the memo (equivalent to advancing past the shipped
                # 60s TTL) forces a re-read for tenant A too.
                _clear_authority_memo()
                _reset_throttle()
                now_denied = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
                assert now_denied.status_code == 403
        finally:
            await pool.execute("DELETE FROM users WHERE id = $1", superadmin["id"])
            await _cleanup_tenant(pool, other_tenant)

    async def test_selection_time_revalidation_after_demotion(self, pool, test_tenant):
        superadmin = await _create_user(role="superadmin", tenant_id=None)
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(superadmin) as client:
                first = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
                assert first.status_code == 200

                await users_service.update_user(superadmin["id"], role="viewer")
                _clear_authority_memo()
                _reset_throttle()
                denied = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
                assert denied.status_code == 403
        finally:
            await _soft_delete_user(pool, superadmin["id"])

    async def test_retenanted_superadmin_is_confined_to_the_fresh_row_tenant(self, pool, test_tenant):
        other_tenant = await _create_tenant(pool)
        # Minted while tenant_id is still NULL — the token keeps claiming
        # NULL for its whole life; only the DB row changes below.
        superadmin = await _create_user(role="superadmin", tenant_id=None)
        try:
            await users_service.update_user(superadmin["id"], tenant_id=test_tenant["id"])
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(superadmin) as client:
                foreign = await client.get("/live-calls", params={"tenant_slug": other_tenant["slug"]})
                assert foreign.status_code == 404

                _reset_throttle()
                own = await client.get("/live-calls", params={"tenant_slug": test_tenant["slug"]})
                assert own.status_code == 200
                assert own.json()["tenant_slug"] == test_tenant["slug"]

                _clear_authority_memo()
                _reset_throttle()
                no_slug = await client.get("/live-calls")
                assert no_slug.status_code == 200
                assert no_slug.json()["tenant_slug"] == test_tenant["slug"]
        finally:
            await pool.execute("UPDATE users SET tenant_id = NULL WHERE id = $1", superadmin["id"])
            await _soft_delete_user(pool, superadmin["id"])
            await _cleanup_tenant(pool, other_tenant)

    async def test_inverse_retenanted_superadmin_token_claims_tenant_row_is_now_null(
        self, pool, test_tenant,
    ):
        # Token claims test_tenant's id; the row is now NULL-tenant
        # superadmin (e.g. detached from its tenant) — must be treated as
        # platform-scoped from the fresh row, not tenant-scoped from the
        # stale claim.
        admin_row = await _create_user(role="admin", tenant_id=test_tenant["id"])
        try:
            await users_service.update_user(admin_row["id"], role="superadmin", tenant_id=None)
            _clear_authority_memo()
            async with _client_as(admin_row) as client:
                # httpx client built from admin_row's own (stale) token is
                # fine here — it's the same token minted above.
                resp = await client.get("/live-calls")
            assert resp.status_code == 400
            assert resp.json() == {"detail": "tenant_slug is required"}
        finally:
            await _soft_delete_user(pool, admin_row["id"])


# ── T7 — AC15 snippet authority, decided on the DB role not the token ───────

class TestSnippetAuthority:
    async def test_snippet_visible_to_admin_withheld_from_supervisor(self, pool, test_tenant, test_admin):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        await _insert_transcript_turn(pool, session_id=session_id, caller_text="the secret caller phrase")
        supervisor = await _create_user(role="supervisor", tenant_id=test_tenant["id"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                admin_resp = await client.get("/live-calls")
            assert admin_resp.status_code == 200
            admin_item = admin_resp.json()["items"][0]
            assert admin_item["transcript_snippet"] == "the secret caller phrase"
            assert admin_item["transcript_withheld"] is False

            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(supervisor) as client:
                sup_resp = await client.get("/live-calls")
            assert sup_resp.status_code == 200
            sup_item = sup_resp.json()["items"][0]
            assert sup_item["transcript_snippet"] is None
            assert sup_item["transcript_withheld"] is True
            assert "the secret caller phrase" not in sup_resp.text
        finally:
            await _cleanup_call(pool, session_id)
            await _soft_delete_user(pool, supervisor["id"])

    async def test_demotion_stops_snippet_only_after_the_shipped_60s_ttl(self, pool, test_tenant, test_admin):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        await _insert_transcript_turn(pool, session_id=session_id, caller_text="only admins should see this")
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                first = await client.get("/live-calls")
                assert first.status_code == 200
                assert first.json()["items"][0]["transcript_snippet"] == "only admins should see this"

                await users_service.update_user(test_admin["user"]["id"], role="supervisor")

                # Still inside the shipped 60s memo TTL — the documented,
                # bounded exposure window (lesson 25: never a shortened one
                # for this assertion).
                _reset_throttle()
                still_admin_view = await client.get("/live-calls")
                assert still_admin_view.status_code == 200
                assert still_admin_view.json()["items"][0]["transcript_snippet"] == "only admins should see this"

                await asyncio.sleep(deps.AUTHORITY_MEMO_TTL_S + 1)

                _reset_throttle()
                after_ttl = await client.get("/live-calls")
                assert after_ttl.status_code == 200
                assert after_ttl.json()["items"][0]["transcript_withheld"] is True
                assert "only admins should see this" not in after_ttl.text
        finally:
            await _cleanup_call(pool, session_id)

    async def test_soft_delete_403s_after_the_shipped_60s_ttl(self, pool, test_tenant, test_admin):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                first = await client.get("/live-calls")
                assert first.status_code == 200

                await _soft_delete_user(pool, test_admin["user"]["id"])

                _reset_throttle()
                still_ok = await client.get("/live-calls")
                assert still_ok.status_code == 200

                await asyncio.sleep(deps.AUTHORITY_MEMO_TTL_S + 1)

                _reset_throttle()
                after_ttl = await client.get("/live-calls")
                assert after_ttl.status_code == 403
        finally:
            await _cleanup_call(pool, session_id)


# ── T8 — KPI stage split, masking, utilization ───────────────────────────

async def _set_max_concurrent_calls(pool, tenant_slug: str, value: int | None) -> None:
    await pool.execute("UPDATE tenants SET max_concurrent_calls = $2 WHERE slug = $1", tenant_slug, value)
    await cache.invalidate(f"tenant:{tenant_slug}")


class TestKpis:
    async def test_stage_split_and_masked_numbers(self, pool, test_tenant):
        await _set_max_concurrent_calls(pool, test_tenant["slug"], 20)
        session_ids = [
            await _insert_call(pool, tenant_slug=test_tenant["slug"], live_stage=None),
            await _insert_call(pool, tenant_slug=test_tenant["slug"], live_stage="ai"),
            await _insert_call(pool, tenant_slug=test_tenant["slug"], live_stage="waiting_for_human"),
            await _insert_call(pool, tenant_slug=test_tenant["slug"], live_stage="human_connected"),
        ]
        try:
            result = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=False)
            kpis = result["kpis"]
            assert kpis["live_calls"] == 4
            assert kpis["ai_only"] == 2  # NULL live_stage COALESCEs to "ai", plus the explicit "ai" row
            assert kpis["waiting_for_human"] == 1
            assert kpis["human_connected"] == 1
            assert kpis["max_concurrent_calls"] == 20
            assert kpis["utilization_pct"] == 20.0  # 4 / 20 * 100

            assert "+14155557788" not in str(result)
            for item in result["items"]:
                assert item["caller_number_masked"] == "+1415•••7788"
        finally:
            for session_id in session_ids:
                await _cleanup_call(pool, session_id)
            await _set_max_concurrent_calls(pool, test_tenant["slug"], None)

    async def test_utilization_is_independent_per_tenant(self, pool, test_tenant):
        other_tenant = await _create_tenant(pool)
        await _set_max_concurrent_calls(pool, test_tenant["slug"], 10)
        await _set_max_concurrent_calls(pool, other_tenant["slug"], 4)
        call_a = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        call_b1 = await _insert_call(pool, tenant_slug=other_tenant["slug"])
        call_b2 = await _insert_call(pool, tenant_slug=other_tenant["slug"])
        try:
            result_a = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=False)
            result_b = await live_calls.get_live_calls(other_tenant["slug"], include_transcript=False)
            assert result_a["kpis"]["utilization_pct"] == 10.0  # 1 / 10
            assert result_b["kpis"]["utilization_pct"] == 50.0  # 2 / 4
        finally:
            await _cleanup_call(pool, call_a)
            await _cleanup_call(pool, call_b1)
            await _cleanup_call(pool, call_b2)
            await _set_max_concurrent_calls(pool, test_tenant["slug"], None)
            await _cleanup_tenant(pool, other_tenant)

    async def test_nullable_cap_has_no_numeric_fallback(self, pool, test_tenant):
        # test_tenant's cap is NULL by default — no default per T1's schema.
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        try:
            result = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=False)
            assert result["kpis"]["max_concurrent_calls"] is None
            assert result["kpis"]["utilization_pct"] is None
        finally:
            await _cleanup_call(pool, session_id)

    async def test_ended_call_disappears_and_counts_decrement(self, pool, test_tenant):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        try:
            before = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=False)
            assert before["kpis"]["live_calls"] == 1
            assert len(before["items"]) == 1

            await pool.execute("UPDATE calls SET ended_at = now() WHERE session_id = $1", session_id)

            after = await live_calls.get_live_calls(test_tenant["slug"], include_transcript=False)
            assert after["kpis"]["live_calls"] == 0
            assert after["items"] == []
        finally:
            await _cleanup_call(pool, session_id)


# ── T9 — rate limit + acquire timeout ────────────────────────────────────

class TestRateLimitAndAcquireTimeout:
    async def test_rate_limit_429s_after_bucket_exhausted(self, pool, test_tenant, test_admin):
        # limit=4 (see LiveCallsThrottle's own docstring for the multiplier
        # and why) — drive exactly the bucket's capacity, then one more.
        _clear_authority_memo()
        _reset_throttle()
        async with _client_as(test_admin["user"]) as client:
            responses = [await client.get("/live-calls") for _ in range(4)]
            fifth = await client.get("/live-calls")
        assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
        assert fifth.status_code == 429
        _reset_throttle()

    async def test_granted_request_is_never_throttled_below_the_bucket_cap(self, test_tenant, test_admin):
        # Sanity check on the other side of the same control: a single poll
        # (the normal 5s cadence) is never itself throttled.
        _clear_authority_memo()
        _reset_throttle()
        async with _client_as(test_admin["user"]) as client:
            resp = await client.get("/live-calls")
        assert resp.status_code == 200
        _reset_throttle()

    async def test_throttle_tolerates_realistic_multi_tab_traffic_without_any_reset(
        self, pool, test_tenant, test_admin,
    ):
        """Lesson 25's own shape: no _reset_throttle() call anywhere in this
        test, and no fixture/fresh-instance sleight of hand either — this
        drives the REAL shared app.state.live_calls_throttle exactly as
        production traffic would hit it. test_admin is a brand-new user
        (fresh per test), so its bucket key has no pre-existing entries;
        nothing here is reset or pre-cleared.

        Simulates one operator's SAME window legitimately containing more
        than one request: tab 1's poll tick, tab 2's own (unsynchronized)
        poll tick, a tenant switch's immediate re-fetch, and a resume's
        immediate re-fetch — four ordinary, non-hammering requests from one
        user landing in one 5s window. This is the review's finding #3
        regression test: limit=1 rejected this exact traffic (every other
        test only passed because it called _reset_throttle() between
        requests); limit=4 tolerates it, while a genuine 5th request in the
        same window still 429s, so the bound still exists."""
        _clear_authority_memo()
        async with _client_as(test_admin["user"]) as client:
            responses = [await client.get("/live-calls") for _ in range(4)]
            assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]

            fifth = await client.get("/live-calls")
            assert fifth.status_code == 429

    async def test_acquire_times_out_when_pool_is_saturated_rather_than_hangs(self, test_tenant):
        # Pre-warm the Redis-cached tenant lookup so the saturated-pool
        # assertion below exercises the acquire timeout itself, not an
        # unrelated (uncapped) wait on tenants_service.get_tenant()'s own
        # cold-cache Postgres read.
        await tenants_service.get_tenant(test_tenant["slug"])

        pool = await db.get_pool()
        held = [await pool.acquire() for _ in range(pool.get_max_size())]
        try:
            with pytest.raises(TimeoutError):
                await live_calls.get_live_calls(
                    test_tenant["slug"], include_transcript=False, acquire_timeout_s=0.05,
                )
        finally:
            for conn in held:
                await pool.release(conn)


# ── T10-T14 — POST /live-calls/{session_id}/interventions ────────────────

async def _post_intervention(client: AsyncClient, session_id: str, *, action: str = "listen",
                              tenant_slug: str | None = None, headers: dict | None = None):
    return await client.post(
        f"/live-calls/{session_id}/interventions",
        json={"action": action, "tenant_slug": tenant_slug},
        headers=headers,
    )


async def _count_audit_rows(pool, entity_id, outcome: str) -> int:
    return await pool.fetchval(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type = 'live_call_intervention' "
        "AND entity_id = $1 AND new_value->>'outcome' = $2",
        entity_id, outcome,
    )


class TestRequestInterventionServiceFunction:
    async def test_cross_tenant_session_id_binds_the_slug_and_returns_none(self, pool, test_tenant):
        other_tenant = await _create_tenant(pool)
        # Agent and call both belong to tenant B; the "caller" (tenant A) is
        # simulated by resolving against test_tenant's own slug/id while the
        # session actually lives under other_tenant.
        session_id = await _insert_call(pool, tenant_slug=other_tenant["slug"])
        user = auth.decode_access_token(auth.create_access_token(
            await _create_user(role="admin", tenant_id=test_tenant["id"]),
        ))
        try:
            result = await live_calls.request_intervention(
                tenant_slug=test_tenant["slug"], tenant_id=test_tenant["id"], session_id=session_id,
                action="listen", user=user, ip_address=None,
            )
            assert result is None
            assert (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM live_call_interventions WHERE session_id = $1", session_id,
                )
            ) == 0
        finally:
            await _cleanup_call(pool, session_id)
            await _cleanup_tenant(pool, other_tenant)

    async def test_write_audit_failure_rolls_back_the_intervention_insert(self, pool, test_tenant, monkeypatch):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        admin_row = await _create_user(role="admin", tenant_id=test_tenant["id"])
        user = auth.decode_access_token(auth.create_access_token(admin_row))
        try:
            async def _raise(*args, **kwargs):
                raise RuntimeError("boom")

            monkeypatch.setattr(audit, "write_audit", _raise)
            with pytest.raises(RuntimeError):
                await live_calls.request_intervention(
                    tenant_slug=test_tenant["slug"], tenant_id=test_tenant["id"], session_id=session_id,
                    action="barge", user=user, ip_address=None,
                )
            assert (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM live_call_interventions WHERE session_id = $1", session_id,
                )
            ) == 0
        finally:
            await _cleanup_call(pool, session_id)
            await _soft_delete_user(pool, admin_row["id"])


class TestInterventionIpAddress:
    async def test_spoofed_non_ip_xff_stores_null(self, pool, test_tenant, test_admin):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                resp = await _post_intervention(
                    client, session_id, headers={"X-Forwarded-For": "not-an-ip, 10.0.0.1"},
                )
            assert resp.status_code == 202, resp.text
            row = await pool.fetchrow(
                "SELECT ip_address FROM live_call_interventions WHERE session_id = $1", session_id,
            )
            assert row["ip_address"] is None
        finally:
            await _cleanup_call(pool, session_id)

    async def test_well_formed_xff_hop_is_stored(self, pool, test_tenant, test_admin):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                resp = await _post_intervention(
                    client, session_id, headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
                )
            assert resp.status_code == 202, resp.text
            row = await pool.fetchrow(
                "SELECT ip_address FROM live_call_interventions WHERE session_id = $1", session_id,
            )
            assert str(row["ip_address"]) == "203.0.113.7"
        finally:
            await _cleanup_call(pool, session_id)


class TestInterventionDenialAuditing:
    async def test_byte_identical_404_across_foreign_nonexistent_and_oversized_session_id(
        self, pool, test_tenant, test_admin,
    ):
        other_tenant = await _create_tenant(pool)
        foreign_session = await _insert_call(pool, tenant_slug=other_tenant["slug"])
        nonexistent_session = f"nope-{uuid.uuid4().hex[:8]}"
        oversized_session = "x" * 300
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                foreign_resp = await _post_intervention(client, foreign_session)
                _reset_throttle()
                nonexistent_resp = await _post_intervention(client, nonexistent_session)
                _reset_throttle()
                oversized_resp = await _post_intervention(client, oversized_session)

            bodies = [foreign_resp, nonexistent_resp, oversized_resp]
            assert all(r.status_code == 404 for r in bodies)
            assert all(r.json() == {"detail": "call not found"} for r in bodies)

            denied_count = await _count_audit_rows(pool, test_tenant["id"], "denied")
            assert denied_count == 1  # T14 aggregates all three denials from one user
        finally:
            await _cleanup_call(pool, foreign_session)
            await _cleanup_tenant(pool, other_tenant)

    async def test_resolve_scope_tenant_mismatch_404_is_also_audited(self, pool, test_tenant, test_admin):
        other_tenant = await _create_tenant(pool)
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                resp = await _post_intervention(
                    client, f"test-live-{uuid.uuid4().hex[:8]}", tenant_slug=other_tenant["slug"],
                )
            # This is _resolve_scope's OWN 404 (a tenant-slug mismatch),
            # distinct from get_live_calls's/request_intervention's query
            # miss — its body is unchanged ("tenant not found"); what T12
            # closes is that it is now AUDITED as a denial too (finding #5),
            # not that its wording changes to match the query-miss case.
            assert resp.status_code == 404
            assert resp.json() == {"detail": "tenant not found"}
            assert await _count_audit_rows(pool, test_tenant["id"], "denied") == 1
        finally:
            await _cleanup_tenant(pool, other_tenant)


# ── T13 — AC9 stale-token re-validation + audit completeness ────────────

class TestInterventionStaleTokenAndAuditCompleteness:
    async def test_soft_delete_after_success_403s_the_replayed_token(self, pool, test_tenant):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        admin_row = await _create_user(role="admin", tenant_id=test_tenant["id"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(admin_row) as client:
                first = await _post_intervention(client, session_id)
                assert first.status_code == 202, first.text

                await _soft_delete_user(pool, admin_row["id"])

                before_count = await pool.fetchval(
                    "SELECT COUNT(*) FROM live_call_interventions WHERE session_id = $1", session_id,
                )
                _reset_throttle()
                replay = await _post_intervention(client, session_id)
                assert replay.status_code == 403
                after_count = await pool.fetchval(
                    "SELECT COUNT(*) FROM live_call_interventions WHERE session_id = $1", session_id,
                )
                assert after_count == before_count
        finally:
            await _cleanup_call(pool, session_id)

    async def test_demotion_after_success_403s_the_replayed_token(self, pool, test_tenant):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        admin_row = await _create_user(role="admin", tenant_id=test_tenant["id"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(admin_row) as client:
                first = await _post_intervention(client, session_id)
                assert first.status_code == 202, first.text

                await users_service.update_user(admin_row["id"], role="viewer")

                _reset_throttle()
                replay = await _post_intervention(client, session_id)
                assert replay.status_code == 403
        finally:
            await _cleanup_call(pool, session_id)
            await _soft_delete_user(pool, admin_row["id"])

    async def test_in_tenant_request_produces_one_paired_intervention_and_audit_row(
        self, pool, test_tenant, test_admin,
    ):
        session_id = await _insert_call(pool, tenant_slug=test_tenant["slug"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                resp = await _post_intervention(client, session_id, action="barge")
            assert resp.status_code == 202, resp.text

            intervention_rows = await pool.fetch(
                "SELECT * FROM live_call_interventions WHERE session_id = $1", session_id,
            )
            assert len(intervention_rows) == 1
            intervention = intervention_rows[0]
            assert intervention["outcome"] == "unavailable"
            assert intervention["action"] == "barge"
            assert intervention["user_email"] == test_admin["user"]["email"]

            audit_rows = await pool.fetch(
                "SELECT * FROM audit_log WHERE entity_type = 'live_call_intervention' "
                "AND entity_id = $1 AND new_value->>'outcome' = 'unavailable'",
                test_tenant["id"],
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["user_id"] == test_admin["user"]["id"]
            assert audit_row["user_email"] == test_admin["user"]["email"]
            assert db.json_col(audit_row["new_value"])["session_id"] == session_id
        finally:
            await _cleanup_call(pool, session_id)


# ── T14 — denial-audit aggregation ────────────────────────────────────────

class TestDenialAuditAggregation:
    async def test_twenty_rapid_denials_from_one_user_aggregate_into_fewer_than_twenty_rows(
        self, pool, test_tenant, test_admin,
    ):
        key = (str(test_admin["user"]["id"]), str(test_tenant["id"]))
        live_calls._denial_audit_windows.pop(key, None)
        before = await _count_audit_rows(pool, test_tenant["id"], "denied")
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                for _ in range(20):
                    _reset_throttle()
                    resp = await _post_intervention(client, f"nope-{uuid.uuid4().hex[:8]}")
                    assert resp.status_code == 404

            after = await _count_audit_rows(pool, test_tenant["id"], "denied")
            assert after - before < 20
            assert after - before == 1
        finally:
            live_calls._denial_audit_windows.pop(key, None)

    async def test_cross_tenant_denials_within_the_window_get_separate_rows(self, pool, test_tenant):
        # The load-bearing regression test for the security-review finding:
        # a superadmin probing tenant A then tenant B within the same 60s
        # window must NOT have tenant B's denial folded into tenant A's row
        # — each tenant gets its own audit trail (AC11 / finding #5).
        other_tenant = await _create_tenant(pool)
        superadmin = await _create_user(role="superadmin", tenant_id=None)
        key_a = (str(superadmin["id"]), str(test_tenant["id"]))
        key_b = (str(superadmin["id"]), str(other_tenant["id"]))
        live_calls._denial_audit_windows.pop(key_a, None)
        live_calls._denial_audit_windows.pop(key_b, None)
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(superadmin) as client:
                resp_a = await _post_intervention(
                    client, f"nope-{uuid.uuid4().hex[:8]}", tenant_slug=test_tenant["slug"],
                )
                assert resp_a.status_code == 404
                _reset_throttle()
                resp_b = await _post_intervention(
                    client, f"nope-{uuid.uuid4().hex[:8]}", tenant_slug=other_tenant["slug"],
                )
                assert resp_b.status_code == 404

            assert await _count_audit_rows(pool, test_tenant["id"], "denied") == 1
            assert await _count_audit_rows(pool, other_tenant["id"], "denied") == 1
        finally:
            live_calls._denial_audit_windows.pop(key_a, None)
            live_calls._denial_audit_windows.pop(key_b, None)
            await _soft_delete_user(pool, superadmin["id"])
            await _cleanup_tenant(pool, other_tenant)

    async def test_granted_requests_are_never_aggregated(self, pool, test_tenant, test_admin):
        session_ids = [await _insert_call(pool, tenant_slug=test_tenant["slug"]) for _ in range(3)]
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                for session_id in session_ids:
                    _reset_throttle()
                    resp = await _post_intervention(client, session_id)
                    assert resp.status_code == 202

            unavailable_count = await _count_audit_rows(pool, test_tenant["id"], "unavailable")
            assert unavailable_count == 3  # one row per successful request, never aggregated
        finally:
            for session_id in session_ids:
                await _cleanup_call(pool, session_id)


# ── T15 — bounded, selective fresh_authority memo ────────────────────────

class TestAuthorityMemoBounds:
    async def test_memo_size_stays_capped_across_many_distinct_scope_keys(
        self, pool, test_tenant, monkeypatch,
    ):
        monkeypatch.setattr(deps, "AUTHORITY_MEMO_MAX_ENTRIES", 5)
        admin_row = await _create_user(role="admin", tenant_id=test_tenant["id"])
        try:
            token_user = auth.decode_access_token(auth.create_access_token(admin_row))
            _clear_authority_memo()
            for i in range(20):
                await deps.fresh_authority(app.state, token_user, f"scope-{i}", ttl_s=60)
            memo = app.state._live_calls_authority_memo
            assert len(memo) <= 5
        finally:
            await _soft_delete_user(pool, admin_row["id"])

    async def test_a_404d_slug_is_not_retained_in_the_memo(self, pool, test_tenant, test_admin):
        bad_slug = f"nope-{uuid.uuid4().hex[:8]}"
        _clear_authority_memo()
        _reset_throttle()
        async with _client_as(test_admin["user"]) as client:
            resp = await client.get("/live-calls", params={"tenant_slug": bad_slug})
        assert resp.status_code == 404

        memo = app.state._live_calls_authority_memo
        assert (str(test_admin["user"]["id"]), bad_slug) not in memo


# ── T25 — soft-deleted own tenant 403s instead of falling through ────────

class TestSoftDeletedOwnTenant:
    async def test_get_live_calls_403s_when_own_tenant_soft_deleted(self, pool, test_tenant, test_admin):
        await tenants_service.soft_delete_tenant(test_tenant["id"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                resp = await client.get("/live-calls")
            assert resp.status_code == 403
        finally:
            await pool.execute("UPDATE tenants SET deleted_at = NULL WHERE id = $1", test_tenant["id"])

    async def test_post_intervention_403s_when_own_tenant_soft_deleted(self, pool, test_tenant, test_admin):
        await tenants_service.soft_delete_tenant(test_tenant["id"])
        try:
            _clear_authority_memo()
            _reset_throttle()
            async with _client_as(test_admin["user"]) as client:
                resp = await _post_intervention(client, f"test-live-{uuid.uuid4().hex[:8]}")
            assert resp.status_code == 403
        finally:
            await pool.execute("UPDATE tenants SET deleted_at = NULL WHERE id = $1", test_tenant["id"])
