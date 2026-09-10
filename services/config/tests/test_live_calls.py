"""
Live Calls Monitoring — T2 (fresh_authority/assert_current_authority), T4
(get_live_calls query shape), T5b (the permanent stale-token-read tripwire),
T6 (tenant isolation / no existence oracle / platform scope / re-tenanted
superadmin), T7 (AC15 snippet authority), T8 (KPI split), T9 (rate limit +
acquire timeout).

Real Postgres + Redis, same convention as test_calls.py/test_console_gate.py
— fixtures test_tenant/test_admin/test_viewer/pool from conftest.py, plus
local helpers below for rows this feature reads/writes that conftest has no
fixture for (calls, agents, transcript_entries, live_call_interventions,
and users at roles/tenant-shapes conftest doesn't already mint).
"""

from __future__ import annotations

import ast
import inspect
import uuid

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from services.config import auth, deps
from services.config import live_calls
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
    app.state._live_calls_authority_memo = {}


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
