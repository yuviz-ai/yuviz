"""
Route-level tests for the OAuth connector surface: role gating, tenant
scoping, the constant failure and 404 bodies, and the response key set. Real
Postgres, a real signed JWT per user (get_current_user re-reads `users`, so
each principal is a real row), and a recording provider transport.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import decrypt_tenant_secret
from libs.tenancy import set_target_tenant
from services.config import auth
from services.toolexec import custom_apis, oauth
from services.toolexec.app import app
from services.toolexec.routers import oauth_connections
from services.toolexec.schemas import ApiKeyConnectRequest, OAuthAuthorizeRequest, OAuthCallbackRequest

CONNECTION_KEYS = {"id", "provider", "status", "account_label", "scopes", "updated_at"}


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_OAUTH_REDIRECT_URI", "https://console.test/integrations/callback")
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_SECRET_REF", "env:TOOLEXEC_TEST_GOOGLE_SECRET")
    monkeypatch.setenv("TOOLEXEC_TEST_GOOGLE_SECRET", "google-client-secret")

    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
            "id_token": "h.eyJlbWFpbCI6ICJhQGIuYyIsICJzdWIiOiAiczEifQ.s",
        })

    monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(_handler))


@pytest_asyncio.fixture(loop_scope="session")
async def world(pool):
    """Tenants A and B, each with an admin, plus a viewer and a supervisor in A."""
    tenants = {}
    for name in ("a", "b"):
        slug = f"oauthr-{name}-{uuid.uuid4().hex[:8]}"
        tenants[name] = str((await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, slug))["id"])
    users = {}
    for key, tenant, role in (("admin_a", "a", "admin"), ("admin_a2", "a", "admin"), ("admin_b", "b", "admin"),
                              ("viewer_a", "a", "viewer"), ("supervisor_a", "a", "supervisor")):
        row = await pool.fetchrow(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES ($1, $2, 'x', $3) "
            "RETURNING id, email, role, tenant_id",
            tenants[tenant], f"{key}-{uuid.uuid4().hex[:8]}@oauthr.test", role,
        )
        token = auth.create_access_token({
            "id": str(row["id"]), "email": row["email"], "role": row["role"],
            "tenant_id": str(row["tenant_id"]), "is_service_account": False,
        })
        users[key] = {"id": str(row["id"]), "headers": {"Authorization": f"Bearer {token}"}}
    yield tenants, users
    set_target_tenant(None)
    ids = list(tenants.values())
    await pool.execute("DELETE FROM oauth_authorization_states WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute(
        "DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE tenant_id = ANY($1::uuid[]))", ids,
    )
    await pool.execute("DELETE FROM users WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _authorize(c, tenant, headers) -> str:
    r = await c.post(f"/tenants/{tenant}/oauth-connections/google/authorize", headers=headers, json={})
    assert r.status_code == 200, r.text
    return parse_qs(urlsplit(r.json()["authorize_url"]).query)["state"][0]


async def _connect(c, tenant, headers) -> dict:
    state = await _authorize(c, tenant, headers)
    r = await c.post(
        f"/tenants/{tenant}/oauth-connections/callback", headers=headers, json={"state": state, "code": "c"},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── disabled by default ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_providers_list_is_empty_until_the_platform_configures_one(world, monkeypatch):
    _tenants, users = world
    async with _client() as c:
        r = await c.get("/oauth-providers", headers=users["admin_a"]["headers"])
        assert r.json() == [{"key": "google", "label": "Google", "auth_kind": "oauth2"}]

        monkeypatch.delenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID")
        r = await c.get("/oauth-providers", headers=users["admin_a"]["headers"])
        assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_authorize_is_refused_for_an_unconfigured_provider(world, monkeypatch):
    tenants, users = world
    monkeypatch.delenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID")
    async with _client() as c:
        r = await c.post(
            f"/tenants/{tenants['a']}/oauth-connections/google/authorize",
            headers=users["admin_a"]["headers"], json={},
        )
    assert r.status_code == 400 and r.json() == {"detail": "oauth_provider_unavailable"}


@pytest.mark.asyncio
async def test_every_route_requires_authentication(world):
    tenants, _users = world
    async with _client() as c:
        for method, path in (
            ("GET", "/oauth-providers"),
            ("GET", f"/tenants/{tenants['a']}/oauth-connections"),
            ("POST", f"/tenants/{tenants['a']}/oauth-connections/google/authorize"),
            ("POST", f"/tenants/{tenants['a']}/oauth-connections/callback"),
            ("POST", f"/tenants/{tenants['a']}/oauth-connections/calcom/api-key"),
            ("DELETE", f"/tenants/{tenants['a']}/oauth-connections/{uuid.uuid4()}"),
        ):
            r = await c.request(method, path, json={} if method == "POST" else None)
            assert r.status_code == 401, (method, path)


# ── role gate and tenant scope ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["viewer_a", "supervisor_a"])
async def test_non_admin_roles_get_403_on_every_write_route(world, who):
    tenants, users = world
    headers = users[who]["headers"]
    async with _client() as c:
        base = f"/tenants/{tenants['a']}/oauth-connections"
        authorize = await c.post(f"{base}/google/authorize", headers=headers, json={})
        callback = await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c"})
        disconnect = await c.delete(f"{base}/{uuid.uuid4()}", headers=headers)
        api_key = await c.post(f"{base}/calcom/api-key", headers=headers, json={"api_key": "cal_live_ok"})
    assert (authorize.status_code, callback.status_code, disconnect.status_code, api_key.status_code) == (
        403, 403, 403, 403,
    )


@pytest.mark.asyncio
async def test_an_admin_of_another_tenant_is_refused_on_every_route(world):
    tenants, users = world
    headers = users["admin_b"]["headers"]
    base = f"/tenants/{tenants['a']}/oauth-connections"
    async with _client() as c:
        responses = [
            await c.get(base, headers=headers),
            await c.post(f"{base}/google/authorize", headers=headers, json={}),
            await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c"}),
            await c.delete(f"{base}/{uuid.uuid4()}", headers=headers),
            await c.post(f"{base}/calcom/api-key", headers=headers, json={"api_key": "cal_live_ok"}),
        ]
    assert [r.status_code for r in responses] == [403, 403, 403, 403, 403]


@pytest.mark.asyncio
async def test_an_admin_demoted_after_authorize_cannot_redeem_the_state(pool, world, monkeypatch):
    """Criterion 4. The state was legitimately issued and is still unexpired, and the JWT still says
    `admin`; only the re-read of the users row can refuse it. Without that, the callback exchanges the
    code and stores a connection on behalf of someone who no longer holds the role."""
    from services.config.deps import forget_user

    tenants, users = world
    headers, user_id = users["admin_a"]["headers"], users["admin_a"]["id"]
    async with _client() as c:
        state = await _authorize(c, tenants["a"], headers)
        await pool.execute("UPDATE users SET role = 'viewer' WHERE id = $1", uuid.UUID(user_id))
        forget_user(app.state, user_id)  # the ~60s memo has lapsed (lesson 27)
        seen = []
        monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(500)))
        r = await c.post(
            f"/tenants/{tenants['a']}/oauth-connections/callback", headers=headers, json={"state": state, "code": "c"},
        )
    assert r.status_code == 403
    assert seen == []
    assert await pool.fetchval("SELECT count(*) FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(tenants["a"])) == 0


@pytest.mark.asyncio
async def test_a_deleted_user_with_a_live_token_is_refused_on_every_write_route(pool, world):
    """Criterion 10. The token is unexpired and signed; the account is gone."""
    from services.config.deps import forget_user

    tenants, users = world
    headers, user_id = users["admin_a2"]["headers"], users["admin_a2"]["id"]
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", uuid.UUID(user_id))
    forget_user(app.state, user_id)
    base = f"/tenants/{tenants['a']}/oauth-connections"
    async with _client() as c:
        statuses = [
            (await c.post(f"{base}/google/authorize", headers=headers, json={})).status_code,
            (await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c"})).status_code,
            (await c.delete(f"{base}/{uuid.uuid4()}", headers=headers)).status_code,
            (await c.post(f"{base}/calcom/api-key", headers=headers, json={"api_key": "k"})).status_code,
        ]
    assert statuses == [401, 401, 401, 401]


# ── request models ────────────────────────────────────────────────────────

def test_request_models_carry_no_tenant_or_redirect_field():
    assert set(OAuthAuthorizeRequest.model_fields) == {"preset_key"}
    assert set(OAuthCallbackRequest.model_fields) == {"state", "code", "accounts_server"}


@pytest.mark.asyncio
async def test_a_redirect_or_tenant_field_in_a_body_is_rejected(world):
    tenants, users = world
    headers = users["admin_a"]["headers"]
    base = f"/tenants/{tenants['a']}/oauth-connections"
    async with _client() as c:
        for extra in ({"redirect_uri": "https://evil.example"}, {"tenant_id": tenants["b"]}):
            assert (await c.post(f"{base}/google/authorize", headers=headers, json=extra)).status_code == 422
            r = await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c", **extra})
            assert r.status_code == 422


# ── the connect flow and its response shape ───────────────────────────────

@pytest.mark.asyncio
async def test_connect_list_and_disconnect_expose_only_the_public_key_set(world):
    tenants, users = world
    headers = users["admin_a"]["headers"]
    async with _client() as c:
        connected = await _connect(c, tenants["a"], headers)
        listed = await c.get(f"/tenants/{tenants['a']}/oauth-connections", headers=headers)
        disconnected = await c.delete(
            f"/tenants/{tenants['a']}/oauth-connections/{connected['id']}", headers=headers,
        )
        listed_after = await c.get(f"/tenants/{tenants['a']}/oauth-connections", headers=headers)

    assert set(connected) == CONNECTION_KEYS
    assert [set(row) for row in listed.json()] == [CONNECTION_KEYS]
    assert "provider_sub" not in listed.text and "enc:t1." not in listed.text
    assert disconnected.status_code == 200 and disconnected.json() == {"disconnected": True}
    assert listed_after.json()[0]["status"] == "disconnected"


@pytest.mark.asyncio
async def test_a_tenants_list_never_contains_another_tenants_connection(world):
    tenants, users = world
    async with _client() as c:
        await _connect(c, tenants["a"], users["admin_a"]["headers"])
        r = await c.get(f"/tenants/{tenants['b']}/oauth-connections", headers=users["admin_b"]["headers"])
    assert r.json() == []


@pytest.mark.asyncio
async def test_every_callback_failure_returns_the_same_400_body(world):
    tenants, users = world
    base = f"/tenants/{tenants['a']}/oauth-connections/callback"
    async with _client() as c:
        replayed = await _authorize(c, tenants["a"], users["admin_a"]["headers"])
        assert (await c.post(base, headers=users["admin_a"]["headers"], json={"state": replayed, "code": "c"})
                ).status_code == 200
        other_admins = await _authorize(c, tenants["a"], users["admin_a"]["headers"])
        bodies = [
            await c.post(base, headers=users["admin_a"]["headers"], json={"state": "forged", "code": "c"}),
            await c.post(base, headers=users["admin_a"]["headers"], json={"state": replayed, "code": "c"}),
            await c.post(base, headers=users["admin_a2"]["headers"], json={"state": other_admins, "code": "c"}),
        ]
    assert [r.status_code for r in bodies] == [400, 400, 400]
    assert {r.content for r in bodies} == {b'{"detail":"oauth_connection_failed"}'}


@pytest.mark.asyncio
async def test_disconnect_404_is_byte_identical_for_an_absent_and_a_foreign_id(world):
    """One principal (tenant B's admin) probes an id that does not exist and
    an id that is tenant A's connection (lesson 2: per-caller invariance)."""
    tenants, users = world
    async with _client() as c:
        a_connection = await _connect(c, tenants["a"], users["admin_a"]["headers"])
        base = f"/tenants/{tenants['b']}/oauth-connections"
        absent = await c.delete(f"{base}/{uuid.uuid4()}", headers=users["admin_b"]["headers"])
        foreign = await c.delete(f"{base}/{a_connection['id']}", headers=users["admin_b"]["headers"])
    assert absent.status_code == foreign.status_code == 404
    assert absent.content == foreign.content


# ── cal.com API-key connection ────────────────────────────────────────────

VALID_CAL_KEY = "cal_live_ok"


class _Recorded(list):
    accept_any = False


@pytest.fixture
def calcom(monkeypatch):
    """Records every provider request. cal.com accepts exactly VALID_CAL_KEY,
    or any key at all once `accept_any` is set."""
    seen = _Recorded()
    monkeypatch.setenv("TOOLEXEC_CALCOM_ENABLED", "1")

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v2/me" and (
            seen.accept_any or request.headers.get("authorization") == f"Bearer {VALID_CAL_KEY}"
        ):
            return httpx.Response(200, json={"status": "success", "data": {"email": "owner@cal.test"}})
        return httpx.Response(401, json={"error": "nope"})

    monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(_handler))
    return seen


@pytest.mark.asyncio
async def test_cal_com_is_dark_until_its_own_env_is_set(world, calcom, monkeypatch):
    # The fixture's provider would accept VALID_CAL_KEY, so only the gate can refuse it.
    monkeypatch.delenv("TOOLEXEC_CALCOM_ENABLED")
    tenants, users = world
    headers = users["admin_a"]["headers"]
    async with _client() as c:
        assert "calcom" not in [p["key"] for p in (await c.get("/oauth-providers", headers=headers)).json()]
        refused = await _put_key(c, tenants["a"], headers)
        monkeypatch.setenv("TOOLEXEC_CALCOM_ENABLED", "1")
        listed = (await c.get("/oauth-providers", headers=headers)).json()
        assert [p["auth_kind"] for p in listed if p["key"] == "calcom"] == ["api_key"]
    assert (refused.status_code, refused.json()) == (400, {"detail": "oauth_connection_failed"})
    assert calcom == []


async def _put_key(c, tenant, headers, key=VALID_CAL_KEY, provider="calcom"):
    return await c.post(f"/tenants/{tenant}/oauth-connections/{provider}/api-key", headers=headers, json={"api_key": key})


@pytest.mark.asyncio
async def test_connecting_a_cal_com_key_verifies_it_and_returns_only_the_public_key_set(pool, world, calcom):
    tenants, users = world
    async with _client() as c:
        r = await _put_key(c, tenants["a"], users["admin_a"]["headers"])
    assert r.status_code == 200
    body = r.json()
    assert set(body) == CONNECTION_KEYS and body["provider"] == "calcom" and body["account_label"] == "owner@cal.test"
    assert not any(k.endswith("_ref") for k in body) and VALID_CAL_KEY not in r.text

    [verify] = calcom
    assert (verify.method, str(verify.url)) == ("GET", "https://api.cal.com/v2/me")
    assert verify.headers["authorization"] == f"Bearer {VALID_CAL_KEY}" and verify.url.query == b""

    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", uuid.UUID(body["id"]))
    assert row["access_token_ref"].startswith("enc:t1.") and VALID_CAL_KEY not in row["access_token_ref"]
    assert (row["auth_kind"], row["status"], row["refresh_token_ref"], row["access_expires_at"], row["api_base_url"]) == (
        "api_key", "connected", None, None, None,
    )


@pytest.mark.asyncio
async def test_a_key_cal_com_rejects_stores_nothing(pool, world, calcom):
    tenants, users = world
    async with _client() as c:
        r = await _put_key(c, tenants["a"], users["admin_a"]["headers"], key="cal_live_wrong")
    assert r.status_code == 400 and r.json() == {"detail": "oauth_connection_failed"}
    assert await pool.fetchval("SELECT count(*) FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(tenants["a"])) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["env:TOOLEXEC_TEST_GOOGLE_SECRET", "k8s:cal-key"])
async def test_a_pointer_shaped_key_is_sent_literally_and_never_resolved(pool, world, calcom, key):
    tenants, users = world
    async with _client() as c:
        refused = await _put_key(c, tenants["a"], users["admin_a"]["headers"], key=key)
        assert refused.status_code == 400
        calcom.accept_any = True  # now the provider would accept it: what is sent is still the literal text
        accepted = await _put_key(c, tenants["a"], users["admin_a"]["headers"], key=key)
    assert accepted.status_code == 200
    assert [r.headers["authorization"] for r in calcom] == [f"Bearer {key}", f"Bearer {key}"]
    stored = await pool.fetchval("SELECT access_token_ref FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(tenants["a"]))
    assert decrypt_tenant_secret(tenants["a"], stored) == key


@pytest.mark.asyncio
async def test_the_api_key_route_answers_one_principal_identically_for_every_unusable_target(world, calcom):
    """Tenant A's admin: another tenant with a real vs a nonexistent provider, and
    its own tenant with an unknown provider vs one that is not a key provider."""
    tenants, users = world
    headers = users["admin_a"]["headers"]
    async with _client() as c:
        other_real = await _put_key(c, tenants["b"], headers, provider="calcom")
        other_fake = await _put_key(c, tenants["b"], headers, provider="nosuch")
        own_unknown = await _put_key(c, tenants["a"], headers, provider="nosuch")
        own_oauth = await _put_key(c, tenants["a"], headers, provider="google")
    assert other_real.status_code == other_fake.status_code == 403 and other_real.content == other_fake.content
    assert own_unknown.status_code == own_oauth.status_code == 400 and own_unknown.content == own_oauth.content
    assert calcom == []


@pytest.mark.asyncio
async def test_an_api_key_connection_never_refreshes_and_disconnect_makes_no_upstream_request(pool, world, calcom):
    tenants, users = world
    headers = users["admin_a"]["headers"]
    async with _client() as c:
        connection = (await _put_key(c, tenants["a"], headers)).json()
        calcom.clear()
        set_target_tenant(tenants["a"])
        token, provider, api_base, kind = await oauth.access_token_for(tenants["a"], connection["id"])
        assert (token, provider.key, api_base, kind) == (VALID_CAL_KEY, "calcom", None, "api_key")
        assert calcom == []  # no verify call, and above all no token-endpoint call

        deleted = await c.delete(f"/tenants/{tenants['a']}/oauth-connections/{connection['id']}", headers=headers)
    assert deleted.status_code == 200 and deleted.json() == {"disconnected": True}
    assert calcom == []


@pytest.mark.asyncio
async def test_a_stale_api_key_row_still_never_reaches_the_refresh_path(pool, world, calcom, monkeypatch):
    tenants, users = world
    async with _client() as c:
        connection = (await _put_key(c, tenants["a"], users["admin_a"]["headers"])).json()

    async def _no_refresh(*args, **kwargs):
        raise AssertionError("an api_key connection must never refresh")

    monkeypatch.setattr(oauth, "_refresh", _no_refresh)
    set_target_tenant(tenants["a"])
    assert (await oauth.access_token_for(tenants["a"], connection["id"]))[0] == VALID_CAL_KEY


def test_the_api_key_request_model_forbids_extra_fields_and_hides_the_secret():
    assert set(ApiKeyConnectRequest.model_fields) == {"api_key"}
    with pytest.raises(ValueError):
        ApiKeyConnectRequest(api_key="k", tenant_id="x")
    assert "cal_live" not in repr(ApiKeyConnectRequest(api_key="cal_live_secret"))


# ── tripwires ─────────────────────────────────────────────────────────────

def test_every_tenant_scoped_handler_awaits_assert_tenant_access():
    """Lesson 38: an un-awaited async guard fails open. The expected set is
    derived from the router itself, so a new route cannot be added unseen."""
    tree = ast.parse(inspect.getsource(oauth_connections))
    handlers = {route.endpoint.__name__ for route in oauth_connections.tenant_scoped_router.routes}
    assert len(handlers) == 5

    awaited, bare = set(), []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "assert_tenant_access":
                bare.append(node)
            if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) == "assert_tenant_access"):
                awaited.add(fn.name)
    assert len(bare) == len(handlers)           # every call site found...
    assert awaited == handlers                  # ...and each one is awaited, in every handler
