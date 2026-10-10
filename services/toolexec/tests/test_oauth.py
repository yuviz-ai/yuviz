"""
OAuth connection core: the authorize/redeem flow, token access with refresh,
and the side-channel-free disconnect. Real Postgres; every provider call goes
to a recording httpx.MockTransport, so "zero token requests" is a recorded
fact, not an absence of evidence.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from starlette.background import BackgroundTasks

from libs.config_sdk.secrets import SecretTenantMismatch, decrypt_tenant_secret, encrypt_tenant_secret
from libs.tenancy import set_target_tenant
from services.toolexec.__main__ import configure_logging
from services.toolexec import auth_schemes, custom_apis, oauth, presets

REDIRECT = "https://console.test/integrations/callback"


@pytest.fixture(autouse=True)
def _provider_env(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_OAUTH_REDIRECT_URI", REDIRECT)
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_SECRET_REF", "env:TOOLEXEC_TEST_GOOGLE_SECRET")
    monkeypatch.setenv("TOOLEXEC_TEST_GOOGLE_SECRET", "google-client-secret")

    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)


def _id_token(email: str, sub: str) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"email": email, "sub": sub}).encode()).rstrip(b"=").decode()
    return f"h.{claims}.s"


class FakeProvider:
    """Records every request. `token` and `revoke` are the per-endpoint
    responders, swapped by individual tests."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token = lambda request: httpx.Response(200, json=token_body())
        self.revoke = lambda request: httpx.Response(200)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        responder = self.revoke if request.url.path == "/revoke" else self.token
        result = responder(request)
        return await result if asyncio.iscoroutine(result) else result

    def form(self, request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def calls_to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def token_body(*, access="at-1", refresh="rt-1", email="admin@acme.test", sub="sub-1", expires_in=3600) -> dict:
    body = {"access_token": access, "expires_in": expires_in, "id_token": _id_token(email, sub)}
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


@pytest.fixture
def fake(monkeypatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(provider.handle))
    return provider


@pytest_asyncio.fixture(loop_scope="session")
async def tenants(pool):
    """Tenants A and B; A has two admins, B has one."""
    made = {}
    for name in ("a", "b"):
        slug = f"oauth-{name}-{uuid.uuid4().hex[:8]}"
        tenant = await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, slug)
        made[name] = str(tenant["id"])
    users = {}
    for key, tenant in (("a", "a"), ("a2", "a"), ("b", "b")):
        row = await pool.fetchrow(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES ($1, $2, 'x', 'admin') RETURNING id",
            made[tenant], f"{key}-{uuid.uuid4().hex[:8]}@oauth.test",
        )
        users[key] = str(row["id"])
    set_target_tenant(made["a"])
    yield made, users
    set_target_tenant(None)
    ids = list(made.values())
    await pool.execute("DELETE FROM oauth_authorization_states WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute(
        "DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE tenant_id = ANY($1::uuid[]))", ids,
    )
    await pool.execute("DELETE FROM users WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)


def _state_of(authorize_url: str) -> str:
    return parse_qs(urlsplit(authorize_url).query)["state"][0]


async def _connect(tenant: str, user: str, fake: FakeProvider, **token_kwargs) -> dict:
    set_target_tenant(tenant)
    url = await oauth.start_authorization(tenant_id=tenant, user_id=user, provider="google", preset_key=None)
    fake.token = lambda request: httpx.Response(200, json=token_body(**token_kwargs))
    return await oauth.complete_authorization(
        tenant_id=tenant, user_id=user, user_email=None, state=_state_of(url), code="auth-code", accounts_server=None,
    )


# ── disabled by default ───────────────────────────────────────────────────

def test_no_provider_is_configured_without_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("TOOLEXEC_OAUTH_"):
            monkeypatch.delenv(name)
    assert oauth.configured_providers() == []


def test_a_provider_with_half_its_env_is_hidden(monkeypatch):
    monkeypatch.delenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_SECRET_REF")
    assert oauth.configured_providers() == []


@pytest.mark.asyncio
async def test_authorize_refuses_an_unconfigured_provider(tenants, fake):
    made, users = tenants
    with pytest.raises(ValueError, match="oauth_provider_unavailable"):
        await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="zoho", preset_key=None)


# ── authorize ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_authorize_stores_a_hashed_state_and_a_tenant_bound_verifier(pool, tenants, fake):
    made, users = tenants
    url = await oauth.start_authorization(
        tenant_id=made["a"], user_id=users["a"], provider="google", preset_key="calendar_booking",
    )
    query = parse_qs(urlsplit(url).query)
    state = query["state"][0]
    row = await pool.fetchrow("SELECT * FROM oauth_authorization_states WHERE tenant_id = $1", uuid.UUID(made["a"]))

    assert row["state_hash"] == hashlib.sha256(state.encode()).hexdigest()
    assert state not in str(dict(row))
    assert row["code_verifier_ref"].startswith("enc:t1.")
    verifier = decrypt_tenant_secret(made["a"], row["code_verifier_ref"])
    with pytest.raises(SecretTenantMismatch):
        decrypt_tenant_secret(made["b"], row["code_verifier_ref"])

    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [REDIRECT]
    assert "https://www.googleapis.com/auth/calendar.events" in query["scope"][0].split(" ")
    assert row["expires_at"] > row["created_at"]


@pytest.mark.asyncio
async def test_authorize_deletes_this_tenants_expired_states_only(pool, tenants, fake):
    made, users = tenants
    for tenant, user in ((made["a"], users["a"]), (made["b"], users["b"])):
        set_target_tenant(tenant)
        await oauth.start_authorization(tenant_id=tenant, user_id=user, provider="google", preset_key=None)
    await pool.execute("UPDATE oauth_authorization_states SET expires_at = now() - interval '1 minute'")

    set_target_tenant(made["a"])
    await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="google", preset_key=None)

    remaining = {str(r["tenant_id"]): r["n"] for r in await pool.fetch(
        "SELECT tenant_id, count(*) AS n FROM oauth_authorization_states "
        "WHERE tenant_id = ANY($1::uuid[]) GROUP BY tenant_id", [made["a"], made["b"]],
    )}
    assert remaining == {made["a"]: 1, made["b"]: 1}  # A's expired row went; B's expired row stayed


@pytest.mark.asyncio
async def test_authorize_rejects_an_unknown_preset(tenants, fake):
    made, users = tenants
    with pytest.raises(ValueError, match="unknown_preset"):
        await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="google", preset_key="nope")


# ── redeem ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exchange_stores_tenant_bound_refs_and_never_puts_a_secret_in_the_url(pool, tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake, access="at-live", refresh="rt-live", sub="sub-A")

    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", connection["id"])
    assert row["access_token_ref"].startswith("enc:t1.") and row["refresh_token_ref"].startswith("enc:t1.")
    assert decrypt_tenant_secret(made["a"], row["access_token_ref"]) == "at-live"
    assert decrypt_tenant_secret(made["a"], row["refresh_token_ref"]) == "rt-live"
    with pytest.raises(SecretTenantMismatch):
        decrypt_tenant_secret(made["b"], row["refresh_token_ref"])
    assert row["account_label"] == "admin@acme.test" and row["provider_sub"] == "sub-A"
    assert set(connection) == {"id", "provider", "status", "account_label", "scopes", "updated_at"}

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert all(r.url.query == b"" for r in fake.requests)
    form = fake.form(request)
    assert form["client_secret"] == "google-client-secret" and form["code"] == "auth-code"
    assert form["redirect_uri"] == REDIRECT and form["grant_type"] == "authorization_code"
    assert form["code_verifier"]


@pytest.mark.asyncio
async def test_reconnect_keeps_the_connection_id(pool, tenants, fake):
    made, users = tenants
    first = await _connect(made["a"], users["a"], fake)
    second = await _connect(made["a"], users["a"], fake, access="at-2", refresh="rt-2")
    assert first["id"] == second["id"]
    assert await pool.fetchval("SELECT count(*) FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(made["a"])) == 1
    actions = await pool.fetch(
        "SELECT action FROM audit_log WHERE entity_type = 'oauth_connection' AND entity_id = $1 ORDER BY changed_at",
        first["id"],
    )
    assert [a["action"] for a in actions] == ["created", "updated"]


async def _fresh_state(tenants, user_key="a", tenant_key="a") -> tuple[str, str]:
    made, users = tenants
    set_target_tenant(made[tenant_key])
    url = await oauth.start_authorization(
        tenant_id=made[tenant_key], user_id=users[user_key], provider="google", preset_key=None,
    )
    return url, _state_of(url)


async def _redeem(tenants, state, *, tenant_key="a", user_key="a", code="auth-code"):
    made, users = tenants
    set_target_tenant(made[tenant_key])
    return await oauth.complete_authorization(
        tenant_id=made[tenant_key], user_id=users[user_key], user_email=None,
        state=state, code=code, accounts_server=None,
    )


@pytest.mark.asyncio
async def test_forged_state_gets_the_fixed_failure_and_no_token_request(tenants, fake):
    await _fresh_state(tenants)
    with pytest.raises(ValueError) as caught:
        await _redeem(tenants, "forged-" + uuid.uuid4().hex)
    assert str(caught.value) == "oauth_connection_failed"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_replayed_state_fails_with_exactly_one_token_request(tenants, fake):
    _url, state = await _fresh_state(tenants)
    await _redeem(tenants, state)
    with pytest.raises(ValueError, match="^oauth_connection_failed$"):
        await _redeem(tenants, state)
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_expired_state_fails_with_no_token_request(pool, tenants, fake):
    _url, state = await _fresh_state(tenants)
    await pool.execute("UPDATE oauth_authorization_states SET expires_at = now() - interval '1 second'")
    with pytest.raises(ValueError, match="^oauth_connection_failed$"):
        await _redeem(tenants, state)
    assert fake.requests == []


@pytest.mark.asyncio
async def test_two_simultaneous_callbacks_with_one_state_make_exactly_one_token_request(tenants, fake):
    """Replay is tested one after the other above. The redeem is a single
    conditional UPDATE, so two callbacks racing on one state must still spend it
    once; a SELECT-then-UPDATE would let both through to the token endpoint."""
    _url, state = await _fresh_state(tenants)

    async def _slow_token(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=token_body())

    fake.token = _slow_token
    results = await asyncio.gather(_redeem(tenants, state), _redeem(tenants, state), return_exceptions=True)

    failures = [r for r in results if isinstance(r, ValueError)]
    assert len(failures) == 1 and str(failures[0]) == "oauth_connection_failed"
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_another_admin_of_the_same_tenant_cannot_redeem_the_state(tenants, fake):
    _url, state = await _fresh_state(tenants, user_key="a")
    with pytest.raises(ValueError, match="^oauth_connection_failed$"):
        await _redeem(tenants, state, user_key="a2")
    assert fake.requests == []


@pytest.mark.asyncio
async def test_another_tenants_state_cannot_be_redeemed_even_with_the_right_user(tenants, fake):
    """B presents A's state AND A's user id. The tenant-bound verifier would
    stop the token request anyway, so the assertion that goes red when
    `tenant_id = $1` is deleted is the last one: A's state survives B's attempt."""
    made, users = tenants
    _url, state = await _fresh_state(tenants)
    set_target_tenant(made["b"])
    with pytest.raises(ValueError, match="^oauth_connection_failed$"):
        await oauth.complete_authorization(
            tenant_id=made["b"], user_id=users["a"], user_email=None,
            state=state, code="auth-code", accounts_server=None,
        )
    assert fake.requests == []
    # B's attempt must not even consume A's state: A can still redeem it.
    await _redeem(tenants, state)
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_a_response_without_a_refresh_token_fails_and_stores_nothing(pool, tenants, fake):
    made, users = tenants
    with pytest.raises(ValueError, match="^oauth_connection_failed$"):
        await _connect(made["a"], users["a"], fake, refresh=None)
    assert await pool.fetchval("SELECT count(*) FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(made["a"])) == 0


@pytest.mark.asyncio
async def test_a_provider_error_never_leaks_its_body(tenants, fake):
    made, users = tenants
    set_target_tenant(made["a"])
    url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="google", preset_key=None)
    fake.token = lambda request: httpx.Response(400, json={"error": "secret-detail-from-provider"})
    with pytest.raises(ValueError) as caught:
        await oauth.complete_authorization(
            tenant_id=made["a"], user_id=users["a"], user_email=None,
            state=_state_of(url), code="c", accounts_server=None,
        )
    assert str(caught.value) == "oauth_connection_failed"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.asyncio
async def test_a_zoho_accounts_server_outside_the_allowlist_makes_no_token_request(tenants, fake, monkeypatch):
    made, users = tenants
    monkeypatch.setenv("TOOLEXEC_OAUTH_ZOHO_CLIENT_ID", "zoho-client-id")
    monkeypatch.setenv("TOOLEXEC_OAUTH_ZOHO_CLIENT_SECRET_REF", "env:TOOLEXEC_TEST_GOOGLE_SECRET")
    set_target_tenant(made["a"])
    url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="zoho", preset_key=None)
    state = _state_of(url)
    for accounts_server in ("https://accounts.evil.example", None):
        with pytest.raises(ValueError, match="^oauth_connection_failed$"):
            await oauth.complete_authorization(
                tenant_id=made["a"], user_id=users["a"], user_email=None,
                state=state, code="c", accounts_server=accounts_server,
            )
        # the state is consumed by the first attempt, so the second proves nothing
        # about the allowlist: issue a fresh one for it.
        url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="zoho", preset_key=None)
        state = _state_of(url)
    assert fake.requests == []


# ── CRM providers: per-tenant origin and non-PKCE ─────────────────────────

@pytest.fixture
def crm_env(monkeypatch):
    for provider in ("SALESFORCE", "HUBSPOT", "ZOHO"):
        monkeypatch.setenv(f"TOOLEXEC_OAUTH_{provider}_CLIENT_ID", f"{provider.lower()}-client-id")
        monkeypatch.setenv(f"TOOLEXEC_OAUTH_{provider}_CLIENT_SECRET_REF", "env:TOOLEXEC_TEST_GOOGLE_SECRET")


async def _connect_crm(
    tenant: str, user: str, fake: FakeProvider, provider: str, *, accounts_server: str | None = None, **claims,
) -> dict:
    set_target_tenant(tenant)
    url = await oauth.start_authorization(tenant_id=tenant, user_id=user, provider=provider, preset_key=None)
    fake.token = lambda request: httpx.Response(200, json={**token_body(), **claims})
    return await oauth.complete_authorization(
        tenant_id=tenant, user_id=user, user_email=None, state=_state_of(url), code="auth-code",
        accounts_server=accounts_server,
    )


async def _api_base(pool, tenant: str, provider: str) -> str | None:
    return await pool.fetchval(
        "SELECT api_base_url FROM oauth_connections WHERE tenant_id = $1 AND provider = $2", uuid.UUID(tenant), provider,
    )


def test_the_crm_providers_are_hidden_until_their_env_is_set(crm_env, monkeypatch):
    assert {"salesforce", "hubspot", "zoho"} <= set(oauth.configured_providers())
    for provider in ("SALESFORCE", "HUBSPOT"):
        monkeypatch.delenv(f"TOOLEXEC_OAUTH_{provider}_CLIENT_SECRET_REF")
    assert "salesforce" not in oauth.configured_providers() and "hubspot" not in oauth.configured_providers()


@pytest.mark.asyncio
async def test_a_salesforce_connect_stores_the_validated_instance_url(pool, tenants, fake, crm_env):
    made, users = tenants
    await _connect_crm(made["a"], users["a"], fake, "salesforce", instance_url="https://acme.my.salesforce.com")
    assert await _api_base(pool, made["a"], "salesforce") == "https://acme.my.salesforce.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("instance_url", [
    "http://acme.my.salesforce.com",
    "https://acme.my.salesforce.com/services",
    "https://acme.my.salesforce.com:8443",
    "https://user@acme.my.salesforce.com",
    "https://acme.my.salesforce.com/",
    "https://evil.example.com",
    "https://acme.my.salesforce.com.evil.example.com",
    "https://notsalesforce.com",
    ["https://acme.my.salesforce.com"],
])
async def test_a_salesforce_connect_with_a_bad_origin_is_refused_and_stores_nothing(
    pool, tenants, fake, crm_env, instance_url,
):
    made, users = tenants
    with pytest.raises(ValueError, match="^oauth_connection_failed$"):
        await _connect_crm(made["a"], users["a"], fake, "salesforce", instance_url=instance_url)
    assert await pool.fetchval("SELECT count(*) FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(made["a"])) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254"])
async def test_an_origin_that_resolves_to_a_private_address_is_refused(
    pool, tenants, fake, crm_env, monkeypatch, address,
):
    async def _private(hostname, port):  # only the instance host: the token endpoint must still resolve
        return [address] if hostname == "acme.my.salesforce.com" else ["93.184.216.34"]

    made, users = tenants
    monkeypatch.setattr(custom_apis, "_resolve_addresses", _private)
    with pytest.raises(ValueError, match="^oauth_connection_failed$"):
        await _connect_crm(made["a"], users["a"], fake, "salesforce", instance_url="https://acme.my.salesforce.com")
    assert await pool.fetchval("SELECT count(*) FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(made["a"])) == 0


@pytest.mark.asyncio
async def test_a_reconnect_whose_token_response_has_no_claim_nulls_the_origin(pool, tenants, fake, crm_env):
    made, users = tenants
    await _connect_crm(made["a"], users["a"], fake, "salesforce", instance_url="https://acme.my.salesforce.com")
    assert await _api_base(pool, made["a"], "salesforce") is not None
    await _connect_crm(made["a"], users["a"], fake, "salesforce")
    assert await _api_base(pool, made["a"], "salesforce") is None


@pytest.mark.asyncio
async def test_a_zoho_data_centre_move_replaces_the_origin(pool, tenants, fake, crm_env):
    made, users = tenants
    await _connect_crm(
        made["a"], users["a"], fake, "zoho", accounts_server="https://accounts.zoho.com",
        api_domain="https://www.zohoapis.com",
    )
    assert await _api_base(pool, made["a"], "zoho") == "https://www.zohoapis.com"
    await _connect_crm(
        made["a"], users["a"], fake, "zoho", accounts_server="https://accounts.zoho.eu",
        api_domain="https://www.zohoapis.eu",
    )
    assert await _api_base(pool, made["a"], "zoho") == "https://www.zohoapis.eu"


@pytest.mark.asyncio
async def test_a_zoho_numeric_zuid_is_stored_as_text(pool, tenants, fake, crm_env):
    # Zoho's userinfo returns ZUID as a number; the provider_sub column is text.
    made, users = tenants
    connection = await _connect_crm(
        made["a"], users["a"], fake, "zoho", accounts_server="https://accounts.zoho.com",
        api_domain="https://www.zohoapis.com", Email="owner@zoho.test", ZUID=60091508656,
    )
    row = await pool.fetchrow(
        "SELECT account_label, provider_sub FROM oauth_connections WHERE id = $1", connection["id"],
    )
    assert row["account_label"] == "owner@zoho.test" and row["provider_sub"] == "60091508656"


@pytest.mark.asyncio
async def test_a_hubspot_flow_has_no_pkce_and_completes_without_a_verifier(pool, tenants, fake, crm_env):
    made, users = tenants
    set_target_tenant(made["a"])
    url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="hubspot", preset_key=None)
    query = parse_qs(urlsplit(url).query)
    assert "code_challenge" not in query and "code_challenge_method" not in query
    assert query["scope"] == ["crm.objects.contacts.read oauth"]  # never empty: HubSpot rejects the install

    row = await pool.fetchrow("SELECT code_verifier_ref FROM oauth_authorization_states WHERE tenant_id = $1", uuid.UUID(made["a"]))
    assert row["code_verifier_ref"].startswith("enc:t1.")  # a sealed filler, the column stays NOT NULL

    fake.token = lambda request: httpx.Response(200, json=token_body())
    connection = await oauth.complete_authorization(
        tenant_id=made["a"], user_id=users["a"], user_email=None, state=_state_of(url), code="c", accounts_server=None,
    )
    assert connection["provider"] == "hubspot"
    assert str(fake.requests[0].url) == "https://api.hubspot.com/oauth/v3/token"  # v1 is deprecated
    assert "code_verifier" not in fake.form(fake.requests[0])
    assert await _api_base(pool, made["a"], "hubspot") is None


@pytest.mark.asyncio
async def test_the_app_required_scopes_from_env_join_every_hubspot_install_url(pool, tenants, fake, crm_env, monkeypatch):
    # HubSpot errors on its consent page when a scope the app requires is missing.
    monkeypatch.setenv("TOOLEXEC_OAUTH_HUBSPOT_REQUIRED_SCOPES", "crm.objects.companies.read  crm.objects.contacts.write")
    made, users = tenants
    set_target_tenant(made["a"])
    url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="hubspot", preset_key=None)
    assert parse_qs(urlsplit(url).query)["scope"] == [
        "crm.objects.companies.read crm.objects.contacts.read crm.objects.contacts.write oauth"
    ]
    # Not stored: an operator who later drops a scope is not pinned to it by the tenant's row.
    stored = await pool.fetchval("SELECT scopes FROM oauth_authorization_states WHERE tenant_id = $1", uuid.UUID(made["a"]))
    assert sorted(stored) == ["crm.objects.contacts.read", "oauth"]

    monkeypatch.setenv("TOOLEXEC_OAUTH_HUBSPOT_REQUIRED_SCOPES", "")
    url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="hubspot", preset_key=None)
    assert parse_qs(urlsplit(url).query)["scope"] == ["crm.objects.contacts.read oauth"]


@pytest.mark.asyncio
async def test_a_dead_hubspot_refresh_token_flips_the_status_and_requires_reconnect(pool, tenants, fake, crm_env):
    # HubSpot answers invalid_request + BAD_REFRESH_TOKEN, not RFC 6749's invalid_grant.
    made, users = tenants
    connection = await _connect_crm(made["a"], users["a"], fake, "hubspot")
    await _expire_access_token(pool, connection["id"])
    fake.token = lambda request: httpx.Response(400, json={"status": "BAD_REFRESH_TOKEN", "error": "invalid_request"})

    with pytest.raises(auth_schemes.ReconnectRequired):
        await oauth.access_token_for(made["a"], str(connection["id"]))
    assert str(fake.requests[-1].url) == "https://api.hubspot.com/oauth/v3/token"
    row = await pool.fetchrow("SELECT status, refresh_token_ref FROM oauth_connections WHERE id = $1", connection["id"])
    assert row["status"] == "reconnect_needed" and row["refresh_token_ref"] is None


def test_every_oauth_provider_asks_for_a_scope_without_a_preset():
    # The account-level Connect sends preset_key=None; an empty scope fails the consent.
    empty = [
        k for k, p in oauth.PROVIDERS.items()
        if p.auth_kind == "oauth2" and not (p.identity_scopes | presets.CONNECT_SCOPES.get(k, frozenset()))
    ]
    assert empty == []


def test_every_crm_bare_connect_carries_its_required_scope():
    # Without its non-identity scope a bare Connect either gets no refresh_token
    # (Salesforce) or is refused by the provider (HubSpot), so each CRM's required
    # scope must ride the account-level Connect.
    for provider, required_scope in (
        ("salesforce", "refresh_token"), ("hubspot", "oauth"), ("zoho", "ZohoCRM.modules.contacts.READ"),
    ):
        bare = oauth.PROVIDERS[provider].identity_scopes | presets.CONNECT_SCOPES.get(provider, frozenset())
        assert required_scope in bare, provider


@pytest.mark.asyncio
async def test_a_salesforce_bare_connect_requests_api_and_refresh(pool, tenants, fake, crm_env):
    made, users = tenants
    set_target_tenant(made["a"])
    url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="salesforce", preset_key=None)
    assert set(parse_qs(urlsplit(url).query)["scope"][0].split()) == {"openid", "api", "refresh_token"}


async def _hubspot_state(tenants, *, tenant_key="a", user_key="a") -> str:
    made, users = tenants
    set_target_tenant(made[tenant_key])
    url = await oauth.start_authorization(
        tenant_id=made[tenant_key], user_id=users[user_key], provider="hubspot", preset_key=None,
    )
    return _state_of(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["tampered", "expired", "reused", "other_user", "other_tenant"])
async def test_a_bad_hubspot_state_gets_the_generic_failure_and_writes_no_row(pool, tenants, fake, crm_env, case):
    """No PKCE verifier to fail on here, so the state row's own predicates are
    the only thing standing between these callers and a token exchange."""
    made, users = tenants
    state = await _hubspot_state(tenants)
    fake.token = lambda request: httpx.Response(200, json=token_body())
    tenant, user = made["a"], users["a"]
    if case == "tampered":
        state = state[:-1] + ("A" if state[-1] != "A" else "B")
    elif case == "expired":
        await pool.execute("UPDATE oauth_authorization_states SET expires_at = now() - interval '1 second'")
    elif case == "reused":
        set_target_tenant(tenant)
        await oauth.complete_authorization(
            tenant_id=tenant, user_id=user, user_email=None, state=state, code="c", accounts_server=None,
        )
        await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(tenant))
        fake.requests.clear()
    elif case == "other_user":
        user = users["a2"]
    elif case == "other_tenant":
        tenant = made["b"]  # with A's user id, so only the tenant predicate refuses it
    set_target_tenant(tenant)

    with pytest.raises(ValueError) as caught:
        await oauth.complete_authorization(
            tenant_id=tenant, user_id=user, user_email=None, state=state, code="c", accounts_server=None,
        )
    assert str(caught.value) == "oauth_connection_failed"
    assert fake.requests == []
    assert await pool.fetchval(
        "SELECT count(*) FROM oauth_connections WHERE tenant_id = ANY($1::uuid[])", [uuid.UUID(made["a"]), uuid.UUID(made["b"])],
    ) == 0


# ── token access and refresh ──────────────────────────────────────────────

async def _expire_access_token(pool, connection_id) -> None:
    await pool.execute(
        "UPDATE oauth_connections SET access_expires_at = now() - interval '1 minute' WHERE id = $1", connection_id,
    )


@pytest.mark.asyncio
async def test_a_fresh_token_is_returned_without_a_provider_call(tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake, access="at-fresh")
    fake.requests.clear()
    token, provider, *_ = await oauth.access_token_for(made["a"], str(connection["id"]))
    assert token == "at-fresh" and provider.key == "google"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_another_tenants_connection_id_requires_reconnect(tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    set_target_tenant(made["b"])
    with pytest.raises(auth_schemes.ReconnectRequired):
        await oauth.access_token_for(made["b"], str(connection["id"]))


@pytest.mark.asyncio
async def test_an_expired_token_is_refreshed_with_a_form_body_and_stored(pool, tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake, refresh="rt-original")
    await _expire_access_token(pool, connection["id"])
    fake.requests.clear()
    fake.token = lambda request: httpx.Response(200, json={"access_token": "at-new", "expires_in": 3600})

    token, *_ = await oauth.access_token_for(made["a"], str(connection["id"]))

    assert token == "at-new"
    (request,) = fake.requests
    assert request.url.query == b""
    assert fake.form(request)["refresh_token"] == "rt-original" and fake.form(request)["grant_type"] == "refresh_token"
    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", connection["id"])
    assert decrypt_tenant_secret(made["a"], row["access_token_ref"]) == "at-new"
    assert decrypt_tenant_secret(made["a"], row["refresh_token_ref"]) == "rt-original"  # not rotated: kept


@pytest.mark.asyncio
async def test_a_refresh_that_lost_the_race_uses_the_winners_token(pool, tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake, refresh="rt-original")
    await _expire_access_token(pool, connection["id"])

    async def _winner_lands_first(request):
        await pool.execute(
            "UPDATE oauth_connections SET access_token_ref = $2, refresh_token_ref = $3, "
            "access_expires_at = now() + interval '1 hour' WHERE id = $1",
            connection["id"], encrypt_tenant_secret(made["a"], "at-winner"),
            encrypt_tenant_secret(made["a"], "rt-winner"),
        )
        return httpx.Response(200, json={"access_token": "at-loser", "expires_in": 3600})

    fake.token = _winner_lands_first
    token, *_ = await oauth.access_token_for(made["a"], str(connection["id"]))

    assert token == "at-winner"
    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", connection["id"])
    assert decrypt_tenant_secret(made["a"], row["access_token_ref"]) == "at-winner"


@pytest.mark.asyncio
async def test_a_refresh_that_lost_the_race_uses_the_winners_token_when_no_refresh_token_rotates(pool, tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake, refresh="rt-original")
    await _expire_access_token(pool, connection["id"])

    async def _winner_lands_first(request):
        await pool.execute(
            "UPDATE oauth_connections SET access_token_ref = $2, access_expires_at = now() + interval '1 hour' "
            "WHERE id = $1", connection["id"], encrypt_tenant_secret(made["a"], "at-winner"),
        )
        return httpx.Response(200, json={"access_token": "at-loser", "expires_in": 3600})

    fake.token = _winner_lands_first
    token, *_ = await oauth.access_token_for(made["a"], str(connection["id"]))

    assert token == "at-winner"
    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", connection["id"])
    assert decrypt_tenant_secret(made["a"], row["access_token_ref"]) == "at-winner"


@pytest.mark.asyncio
async def test_invalid_grant_flips_the_status_and_requires_reconnect(pool, tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    await _expire_access_token(pool, connection["id"])
    fake.token = lambda request: httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(auth_schemes.ReconnectRequired):
        await oauth.access_token_for(made["a"], str(connection["id"]))

    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", connection["id"])
    assert row["status"] == "reconnect_needed"
    assert row["access_token_ref"] is None and row["refresh_token_ref"] is None
    with pytest.raises(auth_schemes.ReconnectRequired):
        await oauth.access_token_for(made["a"], str(connection["id"]))


@pytest.mark.asyncio
async def test_invalid_grant_delivered_to_a_refresh_that_lost_the_race_does_not_flip_the_status(pool, tenants, fake):
    """Microsoft-style rotation: the winner has already replaced the refresh
    token, so the loser's call carried a spent one and the provider says
    invalid_grant. The connection is healthy; flipping it would force a
    reconnect on a working integration."""
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake, refresh="rt-original")
    await _expire_access_token(pool, connection["id"])

    async def _winner_rotates_then_loser_is_refused(request):
        await pool.execute(
            "UPDATE oauth_connections SET access_token_ref = $2, refresh_token_ref = $3, "
            "access_expires_at = now() + interval '1 hour' WHERE id = $1",
            connection["id"], encrypt_tenant_secret(made["a"], "at-winner"), encrypt_tenant_secret(made["a"], "rt-winner"),
        )
        return httpx.Response(400, json={"error": "invalid_grant"})

    fake.token = _winner_rotates_then_loser_is_refused
    token, *_ = await oauth.access_token_for(made["a"], str(connection["id"]))

    assert token == "at-winner"
    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", connection["id"])
    assert row["status"] == "connected"
    assert decrypt_tenant_secret(made["a"], row["refresh_token_ref"]) == "rt-winner"


@pytest.mark.asyncio
async def test_a_5xx_refresh_leaves_the_status_unchanged(pool, tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    await _expire_access_token(pool, connection["id"])
    fake.token = lambda request: httpx.Response(503, text="down")

    with pytest.raises(ValueError, match="^credential_unavailable$") as caught:
        await oauth.access_token_for(made["a"], str(connection["id"]))

    assert not isinstance(caught.value, auth_schemes.ReconnectRequired)
    assert await pool.fetchval("SELECT status FROM oauth_connections WHERE id = $1", connection["id"]) == "connected"


# ── disconnect ────────────────────────────────────────────────────────────

async def _disconnect(tenant, user, connection_id) -> tuple[dict, BackgroundTasks, float]:
    set_target_tenant(tenant)
    background = BackgroundTasks()
    started = time.perf_counter()
    body = await oauth.disconnect(
        tenant_id=tenant, connection_id=str(connection_id), user_id=user, user_email=None,
        background_tasks=background,
    )
    return body, background, time.perf_counter() - started


@pytest.mark.asyncio
async def test_disconnect_clears_the_row_and_audits(pool, tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    body, background, _ = await _disconnect(made["a"], users["a"], connection["id"])
    assert body == {"disconnected": True}
    row = await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", connection["id"])
    assert row["status"] == "disconnected"
    assert row["access_token_ref"] is None and row["refresh_token_ref"] is None
    assert await pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE entity_type = 'oauth_connection' AND entity_id = $1 "
        "AND action = 'updated'", connection["id"]) == 1
    with pytest.raises(auth_schemes.ReconnectRequired):
        await oauth.access_token_for(made["a"], str(connection["id"]))


@pytest.mark.asyncio
async def test_disconnecting_an_absent_or_foreign_connection_raises_the_same_lookup_error(tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    messages = []
    for target in (uuid.uuid4(), connection["id"]):
        with pytest.raises(LookupError) as caught:
            await _disconnect(made["b"], users["b"], target)
        messages.append(str(caught.value))
    assert messages[0] == messages[1]


@pytest.mark.asyncio
async def test_disconnecting_twice_succeeds_and_schedules_no_second_revoke(tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    _, first, _ = await _disconnect(made["a"], users["a"], connection["id"])
    await first()
    body, second, _ = await _disconnect(made["a"], users["a"], connection["id"])
    await second()
    assert body == {"disconnected": True}
    assert len(fake.calls_to("/revoke")) == 1


@pytest.mark.asyncio
async def test_shared_grant_skips_the_revoke_until_the_last_holder_disconnects(tenants, fake):
    made, users = tenants
    a = await _connect(made["a"], users["a"], fake, refresh="rt-A", sub="same-account")
    b = await _connect(made["b"], users["b"], fake, refresh="rt-B", sub="same-account")

    body_a, background_a, _ = await _disconnect(made["a"], users["a"], a["id"])
    await background_a()
    assert body_a == {"disconnected": True}
    assert fake.calls_to("/revoke") == []

    body_b, background_b, _ = await _disconnect(made["b"], users["b"], b["id"])
    await background_b()
    assert body_b == {"disconnected": True}
    (revoke,) = fake.calls_to("/revoke")
    assert fake.form(revoke) == {"token": "rt-B"}
    assert revoke.url.query == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("other_sub, own_sub, expect_revokes", [
    ("sub-other", "sub-own", 1),   # a different account: nothing shared
    (None, None, 1),               # NULL subject: fail closed means revoke
    ("sub-x", None, 1),            # NULL on the disconnecting row only
    (None, "sub-x", 1),            # NULL on the other tenant's row: it cannot match
])
async def test_disconnect_body_is_constant_across_shared_grant_states(
    tenants, fake, other_sub, own_sub, expect_revokes,
):
    made, users = tenants
    b = await _connect(made["b"], users["b"], fake, sub=other_sub or "placeholder")
    if other_sub is None:
        await _null_sub(b["id"])
    a = await _connect(made["a"], users["a"], fake, sub=own_sub or "placeholder")
    if own_sub is None:
        await _null_sub(a["id"])

    body, background, _ = await _disconnect(made["a"], users["a"], a["id"])
    await background()

    assert body == {"disconnected": True}
    assert len(fake.calls_to("/revoke")) == expect_revokes


async def _null_sub(connection_id) -> None:
    from services.toolexec import db

    await (await db.get_pool()).execute("UPDATE oauth_connections SET provider_sub = NULL WHERE id = $1", connection_id)


@pytest.mark.asyncio
async def test_the_revoke_and_the_shared_grant_probe_run_after_the_response(tenants, fake, monkeypatch):
    """The foreground must not wait on, or even issue, the probe and the
    revoke: its duration is then invariant to the shared-grant state."""
    made, users = tenants
    a = await _connect(made["a"], users["a"], fake, sub="same-account")
    probes = []
    real = oauth.platform_conn

    def _counting_platform_conn(*args, **kwargs):
        probes.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(oauth, "platform_conn", _counting_platform_conn)

    async def _slow_revoke(request):
        await asyncio.sleep(0.5)
        return httpx.Response(200)

    fake.revoke = _slow_revoke
    body, background, foreground_s = await _disconnect(made["a"], users["a"], a["id"])

    assert body == {"disconnected": True}
    assert foreground_s < 0.25
    assert probes == [] and fake.calls_to("/revoke") == []

    started = time.perf_counter()
    await background()
    assert time.perf_counter() - started >= 0.5
    assert probes == [1] and len(fake.calls_to("/revoke")) == 1


@pytest.mark.asyncio
async def test_no_secret_reaches_a_log_record_or_an_audit_row_across_callback_refresh_and_disconnect(
    pool, tenants, fake, caplog,
):
    """Design test 5. httpx logs every request URL at INFO, so with it at INFO a
    secret in any query string goes red; the sentinels are first proven to have
    flowed (form bodies, empty queries) so the absence below can fail."""
    made, users = tenants
    caplog.set_level("INFO")
    caplog.set_level("INFO", logger="httpx")
    sentinels = {"access": "AT-SENTINEL-1", "refresh": "RT-SENTINEL-1", "code": "CODE-SENTINEL-1"}
    sentinels["client_secret"] = "google-client-secret"

    set_target_tenant(made["a"])
    url = await oauth.start_authorization(tenant_id=made["a"], user_id=users["a"], provider="google", preset_key=None)
    fake.token = lambda request: httpx.Response(
        200, json=token_body(access=sentinels["access"], refresh=sentinels["refresh"]))
    state = _state_of(url)
    verifier_ref = await pool.fetchval(
        "SELECT code_verifier_ref FROM oauth_authorization_states WHERE tenant_id = $1", uuid.UUID(made["a"]))
    connection = await oauth.complete_authorization(
        tenant_id=made["a"], user_id=users["a"], user_email="admin@acme.test", state=state,
        code=sentinels["code"], accounts_server=None,
    )
    sentinels["verifier"] = decrypt_tenant_secret(made["a"], verifier_ref)

    await _expire_access_token(pool, connection["id"])
    fake.token = lambda request: httpx.Response(200, json={"access_token": "AT-SENTINEL-2", "expires_in": 3600})
    refreshed, *_ = await oauth.access_token_for(made["a"], str(connection["id"]))
    sentinels["access2"] = refreshed
    _body, background, _ = await _disconnect(made["a"], users["a"], connection["id"])
    await background()

    # The values really flowed: each is in a form body, and no URL carried one.
    forms = [fake.form(r) for r in fake.requests]
    assert any(f.get("code") == sentinels["code"] and f.get("code_verifier") == sentinels["verifier"]
               and f.get("client_secret") == sentinels["client_secret"] for f in forms)
    assert any(f.get("refresh_token") == sentinels["refresh"] for f in forms)
    assert any(f.get("token") == sentinels["refresh"] for f in forms)  # the revoke
    assert refreshed == "AT-SENTINEL-2" and len(fake.requests) == 3  # token, refresh, revoke
    assert all(r.url.query == b"" for r in fake.requests)
    assert any(r.name == "httpx" for r in caplog.records)  # httpx was logging, so a URL leak would show

    sinks = [caplog.text, json.dumps(connection, default=str)]
    sinks += [r["t"] for r in await pool.fetch(
        "SELECT row_to_json(a)::text AS t FROM audit_log a WHERE tenant_id = $1", uuid.UUID(made["a"]))]
    for name, value in sentinels.items():
        for sink in sinks:
            assert value not in sink, name


@pytest.mark.asyncio
async def test_a_custom_api_cannot_point_at_another_tenants_connection(pool, tenants, fake):
    """Design test 1(c): the composite FK on (oauth_connection_id, tenant_id) is
    the storage-level fence behind the application predicates, and the only one
    that holds if a handler forgets its own check."""
    import asyncpg

    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    insert = (
        "INSERT INTO custom_apis (tenant_id, name, description, endpoint_url, method, auth_scheme, oauth_connection_id) "
        "VALUES ($1, $2, 'd', 'https://www.googleapis.com/x', 'GET', 'oauth2_authorization_code', $3)"
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(insert, uuid.UUID(made["b"]), f"api_{uuid.uuid4().hex[:8]}", connection["id"])
    try:  # the same insert for the owning tenant is accepted, so the refusal above is the tenant column
        await pool.execute(insert, uuid.UUID(made["a"]), f"api_{uuid.uuid4().hex[:8]}", connection["id"])
    finally:
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = ANY($1::uuid[])", [uuid.UUID(made["a"]), uuid.UUID(made["b"])])


@pytest.mark.asyncio
async def test_a_failing_revoke_is_swallowed_in_the_background(tenants, fake):
    made, users = tenants
    a = await _connect(made["a"], users["a"], fake)
    fake.revoke = lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom"))
    body, background, _ = await _disconnect(made["a"], users["a"], a["id"])
    await background()
    assert body == {"disconnected": True}


# ── revoke_style="path" (HubSpot puts the refresh token in the URL path) ──

_PATH_REVOKE_URL = "https://api.hubapi.com/oauth/v1/refresh-tokens/{token}"


@pytest.fixture
def path_style_revoke(monkeypatch):
    """Google's registry row, re-shaped to revoke the way HubSpot does."""
    google = dataclasses.replace(oauth.PROVIDERS["google"], revoke_url=_PATH_REVOKE_URL, revoke_style="path")
    monkeypatch.setitem(oauth.PROVIDERS, "google", google)


@pytest.fixture
def quiet_http_loggers():
    saved = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    configure_logging()
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


async def _revoke_path_style(tenant: str, token: str, *, provider_sub: str | None = None) -> None:
    await oauth._revoke_upstream(
        tenant_id=tenant, connection_id="c1", provider="google", provider_sub=provider_sub,
        accounts_server=None, old_ref=encrypt_tenant_secret(tenant, token),
    )


@pytest.mark.asyncio
async def test_a_path_style_revoke_puts_the_token_in_the_path_only(tenants, fake, path_style_revoke):
    made, _ = tenants
    await _revoke_path_style(made["a"], "rt-hubspot-1")

    [request] = fake.requests
    assert (request.method, request.url.host, request.url.raw_path) == (
        "DELETE", "api.hubapi.com", b"/oauth/v1/refresh-tokens/rt-hubspot-1",
    )
    assert request.url.query == b"" and request.content == b"" and "token=" not in str(request.url)


@pytest.mark.asyncio
async def test_a_path_style_revoke_encodes_a_token_that_would_split_the_path(tenants, fake, path_style_revoke):
    made, _ = tenants
    await _revoke_path_style(made["a"], "a/b?c=d#e")

    [request] = fake.requests
    assert request.url.raw_path == b"/oauth/v1/refresh-tokens/a%2Fb%3Fc%3Dd%23e"
    assert request.url.query == b"" and request.url.fragment == ""


@pytest.mark.asyncio
async def test_a_failing_path_style_revoke_is_not_retried_and_logs_neither_token_nor_url(
    tenants, fake, path_style_revoke, quiet_http_loggers, caplog,
):
    made, _ = tenants
    token = "rt-secret-5e1c"
    caplog.set_level(logging.DEBUG)

    def _boom(request):  # an error whose text embeds the URL, as a transport's can
        raise httpx.ConnectError(f"cannot reach {request.url}")

    fake.token = _boom
    assert await _revoke_path_style(made["a"], token) is None  # swallowed; nothing escapes

    assert len(fake.requests) == 1
    logged = " ".join(f"{r.getMessage()} {r.exc_text or ''} {r.__dict__}" for r in caplog.records)
    assert "oauth_revoke_failed" in logged
    assert token not in logged and "api.hubapi.com" not in logged and "refresh-tokens" not in logged


@pytest.mark.asyncio
async def test_a_shared_grant_makes_no_path_style_revoke(tenants, fake, path_style_revoke):
    made, users = tenants
    await _connect(made["b"], users["b"], fake, sub="same-account")
    a = await _connect(made["a"], users["a"], fake, sub="same-account")
    fake.requests.clear()

    body, background, _ = await _disconnect(made["a"], users["a"], a["id"])
    await background()

    assert body == {"disconnected": True} and fake.requests == []


@pytest.mark.asyncio
async def test_a_none_style_revoke_makes_no_request_even_with_a_revoke_url(tenants, fake, monkeypatch):
    made, _ = tenants
    google = dataclasses.replace(oauth.PROVIDERS["google"], revoke_style="none")
    monkeypatch.setitem(oauth.PROVIDERS, "google", google)
    await _revoke_path_style(made["a"], "rt-1")
    assert fake.requests == []


# ── auth_schemes.apply ────────────────────────────────────────────────────

def _api(tenant, connection_id, url) -> dict:
    return {
        "id": str(uuid.uuid4()), "tenant_id": tenant, "auth_scheme": "oauth2_authorization_code",
        "auth_config": {}, "oauth_connection_id": str(connection_id), "endpoint_url": url,
        "endpoint_base_source": "literal",
    }


@pytest.mark.asyncio
async def test_apply_sets_the_bearer_for_a_provider_host(tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake, access="at-live")
    headers: dict = {}
    url = "https://www.googleapis.com/calendar/v3/freeBusy"
    injected = await auth_schemes.apply(_api(made["a"], connection["id"], url), headers, {}, effective_url=url)
    assert injected == {"Authorization"} and headers == {"Authorization": "Bearer at-live"}


@pytest.mark.asyncio
async def test_apply_refuses_an_off_provider_host_before_setting_the_header(tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    fake.requests.clear()
    headers: dict = {}
    url = "https://evil.example.com/steal"
    with pytest.raises(ValueError, match="^credential_unavailable$"):
        await auth_schemes.apply(_api(made["a"], connection["id"], url), headers, {}, effective_url=url)
    assert headers == {} and fake.requests == []


@pytest.mark.asyncio
async def test_apply_lets_reconnect_required_through_unwrapped(tenants, fake):
    made, users = tenants
    connection = await _connect(made["a"], users["a"], fake)
    await _disconnect(made["a"], users["a"], connection["id"])
    url = "https://www.googleapis.com/calendar/v3/freeBusy"
    with pytest.raises(auth_schemes.ReconnectRequired):
        await auth_schemes.apply(_api(made["a"], connection["id"], url), {}, {}, effective_url=url)


def test_the_function_local_import_resolves_in_a_fresh_interpreter(tenants):
    """apply() imports oauth lazily to avoid a cycle. An ImportError there
    would be swallowed into credential_unavailable, so a clean run must end
    in ReconnectRequired (no such connection), not in ValueError."""
    script = (
        "import asyncio, sys, uuid\n"
        "from libs.tenancy import set_target_tenant\n"
        "from services.toolexec import auth_schemes\n"
        "tenant = sys.argv[1]\n"
        "set_target_tenant(tenant)\n"
        "api = {'id': 'x', 'tenant_id': tenant, 'auth_scheme': 'oauth2_authorization_code',\n"
        "       'auth_config': {}, 'oauth_connection_id': str(uuid.uuid4()),\n"
        "       'endpoint_url': 'https://www.googleapis.com/x', 'endpoint_base_source': 'literal'}\n"
        "try:\n"
        "    asyncio.run(auth_schemes.apply(api, {}, {}, effective_url=api['endpoint_url']))\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, tenants[0]["a"]], capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert result.stdout.strip() == "ReconnectRequired", result.stderr
