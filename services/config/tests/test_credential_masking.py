"""
Credential masking and the call-site tripwire for the four Config ref tables
(provider_configs, tool_provider_configs, telephony_configs, carriers).

An `enc:` ciphertext is a bearer capability (lesson 43), so no browser-facing
response may carry one. The route set is walked from `app.routes` (lesson 29),
not listed by hand: a new route on these tables breaks the count until it is
either wrapped and added here, or named as not returning a row.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import generate_key
from services.config import auth
from services.config import users as users_service
from services.config.app import app
from services.config.tests.call_sites import find_calls, functions_taking

_TAGS = {"provider_configs", "tool_provider_configs", "telephony_configs", "carriers"}
# Routes on those tags that do not return a row of the four tables.
_NOT_ROW_ROUTES = {
    ("GET", "/providers/{provider_id}/voices"),
    ("POST", "/providers/{provider_id}/preview"),
    ("POST", "/telephony-configs/{config_id}/sync-numbers"),
    ("GET", "/telephony-providers"),
}
_SEEDED = "enc:seeded-sealed-value"
_MASKED = "[stored]"


def _iter_api_routes(routes):
    # app.include_router nests a router's routes behind .original_router.
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)


def _row_routes() -> set[tuple[str, str]]:
    found = set()
    for route in _iter_api_routes(app.routes):
        if not (set(route.tags) & _TAGS):
            continue
        for method in route.methods - {"HEAD", "OPTIONS", "DELETE"}:
            if (method, route.path) not in _NOT_ROW_ROUTES:
                found.add((method, route.path))
    return found


@pytest.fixture(autouse=True)
def _secret_encryption_key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


@pytest_asyncio.fixture
async def seeded(pool, test_tenant):
    tid = test_tenant["id"]
    provider = await pool.fetchval(
        "INSERT INTO provider_configs (tenant_id, name, role, engine, api_key_ref) "
        "VALUES ($1, 'p', 'llm', 'openai', $2) RETURNING id", tid, _SEEDED)
    tool = await pool.fetchval(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine, api_key_ref) "
        "VALUES ($1, 't', 'weather', 'http', $2) RETURNING id", tid, _SEEDED)
    telephony = await pool.fetchval(
        "INSERT INTO telephony_configs (tenant_id, name, provider, credentials) "
        "VALUES ($1, 'v', 'vobiz', $2::jsonb) RETURNING id", tid,
        f'{{"auth_id": "aid", "auth_token": "{_SEEDED}"}}')
    carrier = await pool.fetchval(
        "INSERT INTO carriers (tenant_id, name, provider, auth_token_ref) "
        "VALUES ($1, 'c', 'plivo', $2) RETURNING id", tid, _SEEDED)
    yield {"tenant": str(tid), "provider": str(provider), "tool": str(tool),
           "telephony": str(telephony), "carrier": str(carrier)}
    await pool.execute("DELETE FROM telephony_configs WHERE tenant_id = $1", tid)


async def _principal(pool, test_tenant, role: str, *, tenant: bool, service: bool = False) -> AsyncClient:
    email = f"test-{role}-{uuid.uuid4().hex[:8]}@example.com"
    tenant_id = test_tenant["id"] if tenant else None
    if service:
        user_id = str(uuid.uuid4())
        await pool.execute(
            "INSERT INTO users (id, email, password_hash, role, tenant_id, is_service_account) "
            "VALUES ($1, $2, 'x', $3, NULL, true)", user_id, email, role)
        user = {"id": user_id, "email": email, "role": role, "tenant_id": None, "is_service_account": True}
    else:
        user = await users_service.create_user(
            email=email, password="test-password-not-real", role=role, tenant_id=tenant_id)
    client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {auth.create_access_token(user)}"},
    )
    client.user_id = user["id"]
    return client


async def _drop(pool, client: AsyncClient) -> None:
    await client.aclose()
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", client.user_id)


def _requests(ids: dict) -> dict[tuple[str, str], tuple[str, dict | None, dict | None]]:
    """(method, path template) -> (concrete path, json body, query params)."""
    t = ids["tenant"]
    return {
        ("GET", "/tenants/{tenant_id}/providers"): (f"/tenants/{t}/providers", None, None),
        ("POST", "/tenants/{tenant_id}/providers"): (
            f"/tenants/{t}/providers", {"name": "n", "role": "llm", "engine": "openai", "api_key": "k-plain"}, None),
        ("GET", "/providers/{provider_id}"): (f"/providers/{ids['provider']}", None, None),
        ("PATCH", "/providers/{provider_id}"): (f"/providers/{ids['provider']}", {"model": "m"}, None),
        ("GET", "/tenants/{tenant_id}/tool-providers"): (f"/tenants/{t}/tool-providers", None, None),
        ("POST", "/tenants/{tenant_id}/tool-providers"): (
            f"/tenants/{t}/tool-providers",
            {"name": "n2", "tool_name": "weather", "engine": "http", "api_key": "k-plain"}, None),
        ("GET", "/tool-providers/{tool_provider_config_id}"): (f"/tool-providers/{ids['tool']}", None, None),
        ("PATCH", "/tool-providers/{tool_provider_config_id}"): (
            f"/tool-providers/{ids['tool']}", {"name": "renamed"}, None),
        ("GET", "/tenants/{tenant_id}/telephony-configs"): (f"/tenants/{t}/telephony-configs", None, None),
        ("POST", "/tenants/{tenant_id}/telephony-configs"): (
            f"/tenants/{t}/telephony-configs",
            {"name": "n3", "provider": "vobiz", "credentials": {"auth_id": "a", "auth_token": "tok-plain"}}, None),
        ("GET", "/telephony-configs"): ("/telephony-configs", None, {"provider": "vobiz"}),
        ("GET", "/telephony-configs/{config_id}"): (f"/telephony-configs/{ids['telephony']}", None, None),
        ("PATCH", "/telephony-configs/{config_id}"): (
            f"/telephony-configs/{ids['telephony']}", {"name": "renamed"}, None),
        ("POST", "/telephony-configs/{config_id}/set-default-outbound"): (
            f"/telephony-configs/{ids['telephony']}/set-default-outbound", None, None),
        ("GET", "/tenants/{tenant_id}/carriers"): (f"/tenants/{t}/carriers", None, None),
        ("POST", "/tenants/{tenant_id}/carriers"): (
            f"/tenants/{t}/carriers", {"name": "n4", "provider": "plivo", "auth_token": "tok-plain"}, None),
        ("GET", "/carriers/{carrier_id}"): (f"/carriers/{ids['carrier']}", None, None),
        ("PATCH", "/carriers/{carrier_id}"): (f"/carriers/{ids['carrier']}", {"name": "renamed"}, None),
    }


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _credential_values(body) -> list[str]:
    rows = body if isinstance(body, list) else [body]
    values = []
    for row in rows:
        for key in ("api_key_ref", "auth_token_ref"):
            if key in row:
                values.append(row[key])
        if isinstance(row.get("credentials"), dict) and "auth_token" in row["credentials"]:
            values.append(row["credentials"]["auth_token"])
    return values


def test_walked_routes_match_the_request_table():
    walked = _row_routes()
    assert len(walked) == 18
    assert walked == set(_requests({k: "x" for k in ("tenant", "provider", "tool", "telephony", "carrier")}))


# viewer and supervisor have no write routes; supervisor has no Config surface at all.
_ROLES = {
    "superadmin": dict(tenant=False, writes=True),
    "admin": dict(tenant=True, writes=True),
    "viewer": dict(tenant=True, writes=False),
    "supervisor": dict(tenant=True, writes=False),
}
# Routes only a platform-scoped caller may use (`tenant_id is None`).
_PLATFORM_ONLY = {("GET", "/telephony-configs")}


@pytest.mark.parametrize("role", list(_ROLES))
async def test_no_role_ever_receives_an_enc_string(role, pool, test_tenant, seeded):
    spec = _ROLES[role]
    client = await _principal(pool, test_tenant, role, tenant=spec["tenant"])
    try:
        for (method, template), (path, body, params) in _requests(seeded).items():
            resp = await client.request(method, path, json=body, params=params)
            context = (role, method, template, resp.status_code)
            is_write = method != "GET"
            if role == "supervisor" or (is_write and not spec["writes"]) or (
                (method, template) in _PLATFORM_ONLY and spec["tenant"]
            ):
                assert resp.status_code == 403, context
                continue
            assert resp.status_code in (200, 201), context
            assert not [s for s in _strings(resp.json()) if s.startswith("enc:")], context
            values = _credential_values(resp.json())
            assert values, context  # the credential field is present, so the check below is not vacuous
            assert set(values) == {_MASKED}, context
    finally:
        await _drop(pool, client)


async def test_platform_service_account_still_gets_the_sealed_value(pool, test_tenant, seeded):
    # Proves the fixture contains `enc:` and that the mask is not over-broad:
    # Conversation, vobiz and the DID service read these rows as this principal.
    client = await _principal(pool, test_tenant, "viewer", tenant=False, service=True)
    try:
        for path in (
            f"/providers/{seeded['provider']}", f"/tool-providers/{seeded['tool']}",
            f"/telephony-configs/{seeded['telephony']}", f"/carriers/{seeded['carrier']}",
        ):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert _credential_values(resp.json()) == [_SEEDED], path
    finally:
        await _drop(pool, client)


async def test_tenant_admin_pointer_and_foreign_enc_get_the_same_400(pool, test_tenant, seeded):
    client = await _principal(pool, test_tenant, "admin", tenant=True)
    t = seeded["tenant"]
    bodies = {
        f"/tenants/{t}/providers": {"name": "x", "role": "llm", "engine": "openai"},
        f"/tenants/{t}/tool-providers": {"name": "x", "tool_name": "weather", "engine": "http"},
        f"/tenants/{t}/carriers": {"name": "x", "provider": "plivo"},
    }
    field = {"providers": "api_key_ref", "tool-providers": "api_key_ref", "carriers": "auth_token_ref"}
    try:
        for path, body in bodies.items():
            texts = set()
            for ref in ("env:X", "k8s:/x", "enc:pasted-from-another-tenant"):
                resp = await client.post(path, json={**body, field[path.rsplit("/", 1)[1]]: ref})
                assert resp.status_code == 400, (path, ref, resp.status_code)
                texts.add(resp.text)
            assert len(texts) == 1, path
        for ref in ("env:X", "k8s:/x", "enc:pasted-from-another-tenant"):
            resp = await client.post(f"/tenants/{t}/telephony-configs", json={
                "name": "x", "provider": "vobiz", "credentials": {"auth_id": "a", "auth_token": ref}})
            assert resp.status_code == 400, ref
            assert "credential_ref_not_accepted" in resp.text
    finally:
        await _drop(pool, client)


async def test_platform_scoped_principal_can_store_a_pointer(pool, test_tenant, seeded):
    client = await _principal(pool, test_tenant, "superadmin", tenant=False)
    try:
        resp = await client.post(f"/tenants/{seeded['tenant']}/carriers", json={
            "name": "ptr", "provider": "plivo", "auth_token_ref": "env:PLIVO_AUTH_TOKEN"})
        assert resp.status_code == 201
        assert resp.json()["auth_token_ref"] == "env:PLIVO_AUTH_TOKEN"
    finally:
        await _drop(pool, client)


# Every caller must say, in the call itself, whether pointer schemes are
# allowed. The count makes a new caller break this test until it is read.
_CREDENTIAL_CALLEES = functions_taking("allow_pointer_schemes")
_EXPECTED_CALLEES = 12
_EXPECTED_CALL_SITES = 95  # +3: test_agent_languages (_cartesia + two provider-edit tests)


def test_every_credential_call_site_passes_allow_pointer_schemes():
    assert len(_CREDENTIAL_CALLEES) == _EXPECTED_CALLEES, sorted(_CREDENTIAL_CALLEES)
    calls = find_calls(_CREDENTIAL_CALLEES, "allow_pointer_schemes")
    assert [(p, n, name) for p, n, name, ok in calls if not ok] == []
    assert len(calls) == _EXPECTED_CALL_SITES


def test_allow_pointer_schemes_has_no_default_anywhere():
    """A defaulted keyword lets a new caller omit the decision silently, and the
    call-site count above only catches it when the caller is a direct call."""
    import ast
    from pathlib import Path

    defaulted, seen = [], 0
    for path in sorted(Path(auth.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = node.args.posonlyargs + node.args.args
            defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
            params = list(zip(positional, defaults)) + list(zip(node.args.kwonlyargs, node.args.kw_defaults))
            for arg, default in params:
                if arg.arg == "allow_pointer_schemes":
                    seen += 1
                    if default is not None:
                        defaulted.append(f"{path.name}:{node.name}")
    assert seen == _EXPECTED_CALLEES  # the walk found the same functions the call-site test uses
    assert defaulted == []


# Mechanical inventory of ref-shaped columns (lessons 12, 29, 42): the schema
# is asked what holds a ref, so a fifth column goes red here until it is wired
# or classified. The four wired ones are exactly those the masking and
# round-trip cases above seed and assert on.
_WIRED_REF_COLUMNS = {
    ("provider_configs", "api_key_ref"),
    ("tool_provider_configs", "api_key_ref"),
    ("telephony_configs", "credentials"),
    ("carriers", "auth_token_ref"),
}
# Ref-shaped columns that are not Config credential refs, and why.
_CLASSIFIED_REF_COLUMNS = {
    ("calls", "recording_ref"),                   # storage path, never decrypted
    ("kb_documents", "source_ref"),               # StorageProvider path
    ("carriers", "carrier_account_ref"),          # account identifier; no code decrypts it
    ("oauth_connections", "access_token_ref"),    # toolexec, sealed enc:t1. per tenant
    ("oauth_connections", "refresh_token_ref"),
    ("oauth_authorization_states", "code_verifier_ref"),
}


async def test_every_ref_column_is_wired_or_classified(pool):
    rows = await pool.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND (column_name LIKE '%\\_ref' "
        "OR (table_name = 'telephony_configs' AND column_name = 'credentials'))"
    )
    found = {(r["table_name"], r["column_name"]) for r in rows}
    assert found - _CLASSIFIED_REF_COLUMNS == _WIRED_REF_COLUMNS
    assert len(found - _CLASSIFIED_REF_COLUMNS) == 4
    assert _CLASSIFIED_REF_COLUMNS <= found
