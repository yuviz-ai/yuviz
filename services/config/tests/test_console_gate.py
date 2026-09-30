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

import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from services.config import auth, deps
from services.config import users as users_service
from services.config.app import app

pytestmark = pytest.mark.integration


def _make_user(role: str, *, tenant_id: str | None = "11111111-1111-1111-1111-111111111111",
                is_service_account: bool = False) -> dict:
    return {
        "id": str(uuid.uuid4()), "email": "x@example.com", "role": role, "tenant_id": tenant_id,
        "is_service_account": is_service_account,
    }


class TestConsoleGateModule:
    """No FastAPI app — protects services/config/deps.py for every service
    that imports it, not just this one's routes."""

    async def test_get_authenticated_user_has_no_role_gate(self):
        for role in ("superadmin", "admin", "viewer", "supervisor", "agent"):
            token = auth.create_access_token(_make_user(role))
            authorization = f"Bearer {token}"
            user = await deps.get_authenticated_user(authorization=authorization)
            assert user.role == role

    async def test_get_current_user_403s_non_console_roles(self):
        for role in ("supervisor", "agent"):
            token = auth.create_access_token(_make_user(role))
            authorization = f"Bearer {token}"
            authed = await deps.get_authenticated_user(authorization=authorization)
            with pytest.raises(Exception) as exc_info:
                await deps.get_current_user(user=authed)
            assert exc_info.value.status_code == 403

    async def test_get_current_user_allows_console_roles(self):
        for role in ("superadmin", "admin", "viewer"):
            token = auth.create_access_token(_make_user(role))
            authorization = f"Bearer {token}"
            authed = await deps.get_authenticated_user(authorization=authorization)
            user = await deps.get_current_user(user=authed)
            assert user.role == role

    async def test_get_current_user_allows_service_account_viewer_with_no_tenant(self):
        # scripts/create_service_account.py:33's exact shape — Conversation
        # and Knowledge must not be locked out by this gate.
        token = auth.create_access_token(_make_user("viewer", tenant_id=None, is_service_account=True))
        authed = await deps.get_authenticated_user(authorization=f"Bearer {token}")
        user = await deps.get_current_user(user=authed)
        assert user.role == "viewer"
        assert user.tenant_id is None

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

    def _client_as(self, role: str, tenant_id: str | None):
        token = auth.create_access_token(_make_user(role, tenant_id=tenant_id))
        transport = ASGITransport(app=app)
        return AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )

    @pytest.mark.parametrize("role", ["supervisor", "agent"])
    async def test_non_console_role_403s_on_console_routes(self, role, test_tenant):
        # No bare "list" route exists for carriers/providers/calls — each is
        # only reachable by id (see routers/carriers.py etc) — but the gate
        # runs during dependency resolution, before the handler ever checks
        # whether that id exists, so a made-up uuid still proves the 403
        # comes from the gate, not a 404.
        fake_id = "00000000-0000-0000-0000-000000000000"
        async with self._client_as(role, test_tenant["id"]) as client:
            for path in (
                "/users", f"/providers/{fake_id}", f"/carriers/{fake_id}",
                f"/calls/{fake_id}", "/audit-log",
            ):
                resp = await client.get(path)
                assert resp.status_code == 403, f"{path} -> {resp.status_code}"

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
        async with self._client_as("viewer", test_viewer["user"]["tenant_id"]) as client:
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
        names = _route_names_depending_on(app, deps.get_authenticated_user)
        assert names == {"me", "change_password"}, names


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
