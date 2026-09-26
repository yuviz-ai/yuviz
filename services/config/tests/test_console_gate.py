"""
T6 — the console-role gate. Two kinds of coverage, deliberately not just
one (lesson 9): deps.py is imported by Knowledge, DID and Campaigns as well
as Config, so a guard verified only against the Config app's specific routes
would say nothing about the shared module those other services actually
call. TestConsoleGateModule below calls get_authenticated_user/
get_current_user directly — no FastAPI app involved — so it protects the
module itself; TestConsoleGateApp replays the same matrix against the real
Config app to prove the wiring (require_role, /auth/me, /health, ...) is
correct too.
"""

from __future__ import annotations

import types
import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from services.config import auth, deps
from services.config import users as users_service
from services.config.app import app


def _make_user(role: str, *, tenant_id: str | None = "11111111-1111-1111-1111-111111111111",
                is_service_account: bool = False) -> dict:
    return {
        "id": str(uuid.uuid4()), "email": "x@example.com", "role": role, "tenant_id": tenant_id,
        "is_service_account": is_service_account,
    }


def _fake_request():
    """A minimal stand-in for FastAPI's Request, carrying only what
    get_current_user() actually touches (request.app.state) — used by the
    module-level tests below, which call get_current_user() directly rather
    than through real dependency injection."""
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))


class TestConsoleGateModule:
    """No FastAPI app — protects services/config/deps.py for every service
    that imports it, not just this one's routes."""

    async def test_get_authenticated_user_has_no_role_gate(self):
        for role in ("superadmin", "admin", "viewer", "supervisor", "agent"):
            token = auth.create_access_token(_make_user(role))
            authorization = f"Bearer {token}"
            user = await deps.get_authenticated_user(authorization=authorization)
            assert user.role == role

    async def test_get_current_user_403s_non_console_roles(self, pool, test_tenant):
        # get_current_user() now re-reads the row (fresh_console_authority),
        # so — unlike the fabricated-token tests above — this needs a real
        # user to exist, or the 401 existence check would fire before the
        # role gate ever runs.
        for role in ("supervisor", "agent"):
            user = await users_service.create_user(
                email=f"test-{role}-{uuid.uuid4().hex[:8]}@example.com",
                password="test-password-not-real", role=role, tenant_id=test_tenant["id"],
            )
            try:
                token = auth.create_access_token(user)
                authed = await deps.get_authenticated_user(authorization=f"Bearer {token}")
                with pytest.raises(Exception) as exc_info:
                    await deps.get_current_user(request=_fake_request(), user=authed)
                assert exc_info.value.status_code == 403
            finally:
                await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

    async def test_get_current_user_allows_console_roles(self, pool, test_tenant):
        for role in ("superadmin", "admin", "viewer"):
            user = await users_service.create_user(
                email=f"test-{role}-{uuid.uuid4().hex[:8]}@example.com",
                password="test-password-not-real", role=role, tenant_id=test_tenant["id"],
            )
            try:
                token = auth.create_access_token(user)
                authed = await deps.get_authenticated_user(authorization=f"Bearer {token}")
                current = await deps.get_current_user(request=_fake_request(), user=authed)
                assert current.role == role
            finally:
                await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

    async def test_get_current_user_allows_service_account_viewer_with_no_tenant(self, pool):
        # scripts/create_service_account.py:33's exact shape — Conversation
        # and Knowledge must not be locked out by this gate. is_service_account
        # isn't settable via create_user() (see users.py), so this inserts the
        # row directly, matching that script's own shape.
        user_id = str(uuid.uuid4())
        email = f"test-svc-{uuid.uuid4().hex[:8]}@example.com"
        await pool.execute(
            "INSERT INTO users (id, email, password_hash, role, tenant_id, is_service_account) "
            "VALUES ($1, $2, 'x', 'viewer', NULL, true)",
            user_id, email,
        )
        try:
            token = auth.create_access_token(
                {"id": user_id, "email": email, "role": "viewer", "tenant_id": None, "is_service_account": True},
            )
            authed = await deps.get_authenticated_user(authorization=f"Bearer {token}")
            user = await deps.get_current_user(request=_fake_request(), user=authed)
            assert user.role == "viewer"
            assert user.tenant_id is None
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user_id)

    async def test_get_current_user_rejects_soft_deleted_user(self, pool, test_tenant):
        # The actual regression this closes (07-qa-report.md finding #1): a
        # soft-deleted user's still-validly-signed JWT must lose console
        # access immediately, not just at /auth/me.
        user = await users_service.create_user(
            email=f"test-offboarded-{uuid.uuid4().hex[:8]}@example.com",
            password="test-password-not-real", role="admin", tenant_id=test_tenant["id"],
        )
        token = auth.create_access_token(user)
        authed = await deps.get_authenticated_user(authorization=f"Bearer {token}")
        # Sanity check: the token works before the delete.
        current = await deps.get_current_user(request=_fake_request(), user=authed)
        assert current.role == "admin"

        await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

        with pytest.raises(Exception) as exc_info:
            await deps.get_current_user(request=_fake_request(), user=authed)
        assert exc_info.value.status_code == 401

    def test_console_roles_is_an_explicit_allowlist_of_users_role_check(self):
        # Every role the CHECK constraint accepts must be explicitly
        # classified as console or non-console — a role added there and
        # forgotten here should fail loudly, not pass silently.
        all_roles = {"superadmin", "admin", "supervisor", "agent", "viewer"}
        non_console = {"supervisor", "agent"}
        assert deps.CONSOLE_ROLES == all_roles - non_console


class TestConsoleGateApp:
    """Replays the same matrix through the real Config app + routes."""

    @pytest.fixture
    async def anon_client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    def _client_with_token(self, token: str) -> AsyncClient:
        transport = ASGITransport(app=app)
        return AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )

    @pytest.mark.parametrize("role", ["supervisor", "agent"])
    async def test_non_console_role_403s_on_console_routes(self, role, pool, test_tenant):
        # No bare "list" route exists for carriers/providers/calls — each is
        # only reachable by id (see routers/carriers.py etc) — but the gate
        # runs during dependency resolution, before the handler ever checks
        # whether that id exists, so a made-up uuid still proves the 403
        # comes from the gate, not a 404. A real user row is required now
        # that get_current_user() re-reads it (fresh_console_authority) —
        # a fabricated token for a nonexistent id would 401 before the role
        # gate ever ran.
        fake_id = "00000000-0000-0000-0000-000000000000"
        user = await users_service.create_user(
            email=f"test-{role}-{uuid.uuid4().hex[:8]}@example.com",
            password="test-password-not-real", role=role, tenant_id=test_tenant["id"],
        )
        try:
            token = auth.create_access_token(user)
            async with self._client_with_token(token) as client:
                for path in (
                    "/users", f"/providers/{fake_id}", f"/carriers/{fake_id}",
                    f"/calls/{fake_id}", "/audit-log",
                ):
                    resp = await client.get(path)
                    assert resp.status_code == 403, f"{path} -> {resp.status_code}"
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

    @pytest.mark.parametrize("role", ["supervisor", "agent"])
    async def test_non_console_role_still_reaches_self_service_auth(self, role, pool, test_tenant):
        # A real user row (not just a signed token) so /auth/me has
        # something to return and /auth/change-password has a real password
        # to verify against — T6's own criterion is "200/204 from both",
        # which a mere "not 403" can't distinguish from a 404/401.
        password = "test-password-not-real"
        email = f"test-{role}-{uuid.uuid4().hex[:8]}@example.com"
        user = await users_service.create_user(
            email=email, password=password, role=role, tenant_id=test_tenant["id"],
        )
        try:
            token = auth.create_access_token(user)
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test",
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                me_resp = await client.get("/auth/me")
                assert me_resp.status_code == 200, me_resp.text
                assert me_resp.json()["email"] == email

                change_resp = await client.post(
                    "/auth/change-password",
                    json={"current_password": password, "new_password": "a-new-real-password"},
                )
                assert change_resp.status_code == 204, change_resp.text
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

    async def test_viewer_still_reads_console_routes(self, test_viewer):
        # Uses the fixture's own real token (a real DB row backs it) rather
        # than a fabricated one — get_current_user() now requires the row to
        # exist.
        async with self._client_with_token(test_viewer["token"]) as client:
            resp = await client.get("/users")
            assert resp.status_code == 200

    async def test_unauthenticated_routes_reach_their_handlers(self, anon_client):
        assert (await anon_client.get("/health")).status_code != 401
        assert (await anon_client.get("/auth/setup-status")).status_code != 401
        # login's own 401 for bad credentials is a real business-logic
        # result, not the gate — so assert its detail is the login one, not
        # the gate's "missing or malformed Authorization header".
        login_resp = await anon_client.post("/auth/login", json={"email": "x", "password": "y"})
        assert login_resp.json()["detail"] == "invalid email or password"
        assert (await anon_client.post("/auth/bootstrap", json={"email": "x@x.com", "password": "12345678"})).status_code != 401

    def test_exactly_two_routes_depend_on_get_authenticated_user(self):
        # Named allowlist, not a bare literal: route name -> the gate beyond
        # "decode-or-401" it's allowed to carry. None means unrestricted (any
        # authenticated role) — /auth/me and /auth/change-password are
        # deliberately reachable by every role, including supervisor/agent
        # (see module docstring). A THIRD route landing a bare
        # Depends(get_authenticated_user) — bypassing both this allowlist and
        # CONSOLE_ROLES entirely — must fail here rather than pass silently.
        names = _route_names_depending_on(app, deps.get_authenticated_user)
        assert names == set(DIRECT_AUTHENTICATED_USER_ALLOWLIST), names

    def test_live_calls_routes_match_role_allowlist(self):
        # Live Calls routes don't depend on get_authenticated_user directly
        # (they go through require_live_calls_operator()'s own inner check,
        # same indirection get_current_user already uses) — so this is a
        # separate allowlist, keyed on the operator gate's shared code
        # object rather than route names, and it fails if a route is added
        # under that gate without an allowlist entry, or if LIVE_CALLS_ROLES
        # itself ever grows to include viewer.
        names = _route_names_depending_on_code(app, _LIVE_CALLS_OPERATOR_CODE)
        assert names == set(LIVE_CALLS_ROLE_ALLOWLIST), names
        for role_set in LIVE_CALLS_ROLE_ALLOWLIST.values():
            assert role_set == deps.LIVE_CALLS_ROLES == {"superadmin", "admin", "supervisor"}
            assert "viewer" not in role_set


def _iter_api_routes(routes):
    """Recurses through FastAPI's _IncludedRouter wrapping (app.include_router
    nests the router's own routes behind .original_router rather than
    flattening them into app.routes directly)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)


def _route_names_depending_on(fastapi_app: FastAPI, dep) -> set[str]:
    """Direct dependents only — every route that depends on get_current_user
    also *transitively* reaches get_authenticated_user (get_current_user is
    built on top of it), so recursing would make this assertion trivially
    true for the whole app instead of the two routes that name it directly."""
    names: set[str] = set()
    for route in _iter_api_routes(fastapi_app.routes):
        if any(sub.call is dep for sub in route.dependant.dependencies):
            names.add(route.name)
    return names


# T3: named allowlist for _route_names_depending_on(app, get_authenticated_user)
# — a new entry (route -> expected gate) can be added here without touching
# the assertion shape above.
DIRECT_AUTHENTICATED_USER_ALLOWLIST: dict[str, frozenset[str] | None] = {
    "me": None,
    "change_password": None,
}


def _route_names_depending_on_code(fastapi_app: FastAPI, code) -> set[str]:
    """Same "direct dependents only" shape as _route_names_depending_on, but
    keyed on a dependency function's __code__ rather than its identity:
    require_live_calls_operator() is a factory called once per route
    (Depends(require_live_calls_operator())), so each route's inner check
    closure is a distinct object — but every closure it returns shares the
    same compiled function body, so __code__ identity still finds all of
    them without recursing into get_current_user's own indirection."""
    names: set[str] = set()
    for route in _iter_api_routes(fastapi_app.routes):
        if any(getattr(sub.call, "__code__", None) is code for sub in route.dependant.dependencies):
            names.add(route.name)
    return names


_LIVE_CALLS_OPERATOR_CODE = deps.require_live_calls_operator().__code__

# T5a/T12: route -> expected role set for every route gated by
# require_live_calls_operator().
LIVE_CALLS_ROLE_ALLOWLIST: dict[str, frozenset[str]] = {
    "get_live_calls": frozenset({"superadmin", "admin", "supervisor"}),
    "request_intervention": frozenset({"superadmin", "admin", "supervisor"}),
}
