"""Console-role gate, tested both on deps.py directly (shared by other services) and through the Config app."""

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
    """Minimal Request stand-in carrying only request.app.state."""
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
        # get_current_user() re-reads the row, so a real user is needed to reach the role gate.
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
        # create_service_account.py's shape; inserted directly since create_user() can't set the flag.
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
        # A soft-deleted user's still-valid JWT loses console access immediately.
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
        # A role added to the CHECK constraint must be classified here, or this fails.
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
        # The gate runs before the handler, so a made-up id still proves the 403 is the gate's.
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
                assert change_resp.status_code == 200, change_resp.text
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

    async def test_viewer_still_reads_console_routes(self, test_viewer):
        async with self._client_with_token(test_viewer["token"]) as client:
            resp = await client.get("/users")
            assert resp.status_code == 200

    async def test_unauthenticated_routes_reach_their_handlers(self, anon_client):
        assert (await anon_client.get("/health")).status_code != 401
        # login's 401 must be its own, not the gate's.
        login_resp = await anon_client.post("/auth/login", json={"email": "x", "password": "y"})
        assert login_resp.json()["detail"] == "invalid email or password"
        assert (await anon_client.post("/auth/register", json={"email": "x@x.com"})).status_code != 401

    def test_only_allowlisted_routes_depend_on_get_authenticated_user(self):
        # Any other route on bare get_authenticated_user would bypass
        # CONSOLE_ROLES; the allowlisted self-service routes serve every role.
        names = _route_names_depending_on(app, deps.get_authenticated_user)
        assert names == set(DIRECT_AUTHENTICATED_USER_ALLOWLIST), names

    def test_live_calls_routes_match_role_allowlist(self):
        # Fails if a route is added under the operator gate without an entry, or viewer is added.
        names = _route_names_depending_on_code(app, _LIVE_CALLS_OPERATOR_CODE)
        assert names == set(LIVE_CALLS_ROLE_ALLOWLIST), names
        for role_set in LIVE_CALLS_ROLE_ALLOWLIST.values():
            assert role_set == deps.LIVE_CALLS_ROLES == {"superadmin", "admin", "supervisor"}
            assert "viewer" not in role_set


def _iter_api_routes(routes):
    """Yields APIRoutes, recursing into included routers' .original_router."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)


def _route_names_depending_on(fastapi_app: FastAPI, dep) -> set[str]:
    """Direct dependents only; transitively every get_current_user route would match."""
    names: set[str] = set()
    for route in _iter_api_routes(fastapi_app.routes):
        if any(sub.call is dep for sub in route.dependant.dependencies):
            names.add(route.name)
    return names


DIRECT_AUTHENTICATED_USER_ALLOWLIST: dict[str, frozenset[str] | None] = {
    "me": None,
    "change_password": None,
    "change_email": None,
    "confirm_email_change": None,
}


def _route_names_depending_on_code(fastapi_app: FastAPI, code) -> set[str]:
    """Like _route_names_depending_on, keyed on __code__ since factory closures differ per route."""
    names: set[str] = set()
    for route in _iter_api_routes(fastapi_app.routes):
        if any(getattr(sub.call, "__code__", None) is code for sub in route.dependant.dependencies):
            names.add(route.name)
    return names


_LIVE_CALLS_OPERATOR_CODE = deps.require_live_calls_operator().__code__

# Route -> expected role set for every route gated by require_live_calls_operator().
LIVE_CALLS_ROLE_ALLOWLIST: dict[str, frozenset[str]] = {
    "get_live_calls": frozenset({"superadmin", "admin", "supervisor"}),
    "request_intervention": frozenset({"superadmin", "admin", "supervisor"}),
}
