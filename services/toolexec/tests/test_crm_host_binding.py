"""
Host binding and per-tenant origin isolation for connector tokens. Real
Postgres; connections are seeded directly so the binding is exercised without
the connect flow. Any request that would leave the process is recorded by a
MockTransport, so "no request" is a recorded fact.
"""

from __future__ import annotations

import datetime
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

from libs.config_sdk.secrets import encrypt_tenant_secret
from libs.tenancy import set_target_tenant
from services.toolexec import auth_schemes, custom_apis, oauth

ORIGIN_A = "https://a-org.zohoapis.eu"
ORIGIN_B = "https://b-org.zohoapis.in"


@pytest.fixture(autouse=True)
def _no_provider_traffic(monkeypatch):
    sent: list[httpx.Request] = []

    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    def _handle(request):
        sent.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)
    monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(_handle))
    return sent


@pytest_asyncio.fixture(loop_scope="session")
async def tenants(pool):
    made = {}
    for name in ("a", "b"):
        slug = f"crmhost-{name}-{uuid.uuid4().hex[:8]}"
        row = await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, slug)
        made[name] = str(row["id"])
    set_target_tenant(made["a"])
    yield made
    set_target_tenant(None)
    ids = list(made.values())
    await pool.execute("DELETE FROM custom_apis WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)


async def _seed(pool, tenant: str, api_base_url: str | None, token: str) -> str:
    row = await pool.fetchrow(
        "INSERT INTO oauth_connections (tenant_id, provider, status, access_token_ref, access_expires_at, "
        "                               refresh_token_ref, api_base_url) "
        "VALUES ($1, 'zoho', 'connected', $2, $3, $4, $5) RETURNING id",
        uuid.UUID(tenant), encrypt_tenant_secret(tenant, token),
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        encrypt_tenant_secret(tenant, f"rt-{token}"), api_base_url,
    )
    return str(row["id"])


def _api(tenant: str, connection_id: str, source: str, endpoint_url: str) -> dict:
    return {
        "id": str(uuid.uuid4()), "tenant_id": tenant, "auth_scheme": "oauth2_authorization_code",
        "auth_config": {}, "oauth_connection_id": connection_id, "endpoint_url": endpoint_url,
        "endpoint_base_source": source,
    }


async def _apply(api: dict, effective_url: str) -> dict:
    headers: dict = {}
    await auth_schemes.apply(api, headers, {}, effective_url=effective_url)
    return headers


# ── provider_host_allowed ─────────────────────────────────────────────────

@pytest.mark.parametrize("source, host, base, expected", [
    ("literal", "www.zohoapis.com", None, True),
    ("literal", "www.zohoapis.com", ORIGIN_A, True),        # an origin never widens a literal row
    ("literal", "a-org.zohoapis.eu", ORIGIN_A, False),
    ("literal", "evil.example.com", ORIGIN_A, False),
    ("oauth_connection", "a-org.zohoapis.eu", ORIGIN_A, True),
    ("oauth_connection", "other.zohoapis.eu", ORIGIN_A, False),   # a legitimate suffix is not the stored origin
    ("oauth_connection", "www.zohoapis.com", ORIGIN_A, False),    # nor is a fixed host
    ("oauth_connection", "a-org.zohoapis.eu", None, False),
    ("oauth_connection", None, ORIGIN_A, False),
])
def test_provider_host_allowed(source, host, base, expected):
    assert oauth.provider_host_allowed(oauth.PROVIDERS["zoho"], host, base, base_source=source) is expected


# ── (a) one connection serves both row kinds ─────────────────────────────

@pytest.mark.asyncio
async def test_one_connection_serves_a_literal_row_and_a_composed_row(pool, tenants):
    conn_id = await _seed(pool, tenants["a"], ORIGIN_A, "tok-a")
    literal = _api(tenants["a"], conn_id, "literal", "https://www.zohoapis.com/crm/v6/Events")
    composed = _api(tenants["a"], conn_id, "oauth_connection", "/crm/v6/Contacts/search")

    assert await _apply(literal, literal["endpoint_url"]) == {"Authorization": "Bearer tok-a"}
    assert await _apply(composed, ORIGIN_A + composed["endpoint_url"]) == {"Authorization": "Bearer tok-a"}


# ── (b) the dialed URL is what is checked ────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("dialed", ["https://evil.example.com/steal", "https://other.zohoapis.eu/x"])
async def test_a_composed_row_dialing_anything_but_the_stored_origin_gets_no_header(pool, tenants, dialed):
    conn_id = await _seed(pool, tenants["a"], ORIGIN_A, "tok-a")
    composed = _api(tenants["a"], conn_id, "oauth_connection", "/crm/v6/Contacts/search")
    headers: dict = {}

    with pytest.raises(ValueError, match="^credential_unavailable$"):
        await auth_schemes.apply(composed, headers, {}, effective_url=dialed)
    assert headers == {}


# ── (c) tenants dial their own origin with their own token ───────────────

@pytest.mark.asyncio
async def test_each_tenant_dials_its_own_origin_with_its_own_token(pool, tenants):
    conn_a = await _seed(pool, tenants["a"], ORIGIN_A, "tok-a")
    conn_b = await _seed(pool, tenants["b"], ORIGIN_B, "tok-b")

    assert await oauth.connection_api_base(tenants["a"], conn_a) == ORIGIN_A
    assert await oauth.connection_api_base(tenants["b"], conn_b) == ORIGIN_B
    for tenant, conn, origin, token in ((tenants["a"], conn_a, ORIGIN_A, "tok-a"), (tenants["b"], conn_b, ORIGIN_B, "tok-b")):
        api = _api(tenant, conn, "oauth_connection", "/crm/v6/Contacts/search")
        assert await _apply(api, origin + api["endpoint_url"]) == {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_connection_api_base_never_returns_another_tenants_origin(pool, tenants):
    conn_b = await _seed(pool, tenants["b"], ORIGIN_B, "tok-b")
    assert await oauth.connection_api_base(tenants["a"], conn_b) is None


@pytest.mark.asyncio
async def test_access_token_for_never_opens_another_tenants_connection(pool, tenants):
    conn_b = await _seed(pool, tenants["b"], ORIGIN_B, "tok-b")
    with pytest.raises(auth_schemes.ReconnectRequired):
        await oauth.access_token_for(tenants["a"], conn_b)


# ── (d) the composite FK ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_custom_api_cannot_name_another_tenants_connection(pool, tenants):
    conn_a = await _seed(pool, tenants["a"], ORIGIN_A, "tok-a")
    insert = (
        "INSERT INTO custom_apis (tenant_id, name, description, endpoint_url, method, auth_scheme, "
        "                         oauth_connection_id, endpoint_base_source) "
        "VALUES ($1, $2, 'd', '/crm/v6/Contacts/search', 'GET', 'oauth2_authorization_code', $3, 'oauth_connection')"
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(insert, uuid.UUID(tenants["b"]), f"api_{uuid.uuid4().hex[:8]}", uuid.UUID(conn_a))
    # the same insert for the owning tenant is accepted, so the refusal above is the tenant column
    await pool.execute(insert, uuid.UUID(tenants["a"]), f"api_{uuid.uuid4().hex[:8]}", uuid.UUID(conn_a))


# ── (e) a missing origin fails closed for composed rows only ─────────────

@pytest.mark.asyncio
async def test_a_null_origin_fails_the_composed_row_but_not_a_literal_row(pool, tenants):
    conn_id = await _seed(pool, tenants["a"], None, "tok-a")
    composed = _api(tenants["a"], conn_id, "oauth_connection", "/crm/v6/Contacts/search")
    literal = _api(tenants["a"], conn_id, "literal", "https://www.zohoapis.com/crm/v6/Events")

    assert await oauth.connection_api_base(tenants["a"], conn_id) is None  # the executor's reconnect_required trigger
    with pytest.raises(ValueError, match="^credential_unavailable$"):
        await auth_schemes.apply(composed, {}, {}, effective_url="https://www.zohoapis.com/crm/v6/Contacts/search")
    assert await _apply(literal, literal["endpoint_url"]) == {"Authorization": "Bearer tok-a"}


# ── (f) post_json keeps its literal binding ──────────────────────────────

@pytest.mark.asyncio
async def test_post_json_refuses_a_host_outside_api_hosts_even_when_it_is_the_stored_origin(
    pool, tenants, _no_provider_traffic,
):
    conn_id = await _seed(pool, tenants["a"], ORIGIN_A, "tok-a")

    with pytest.raises(ValueError, match="^credential_unavailable$"):
        await oauth.post_json(tenants["a"], conn_id, ORIGIN_A + "/crm/v6/Events", {})
    assert _no_provider_traffic == []

    await oauth.post_json(tenants["a"], conn_id, "https://www.zohoapis.com/crm/v6/Events", {})
    assert [r.url.host for r in _no_provider_traffic] == ["www.zohoapis.com"]  # the refusal above was the host
