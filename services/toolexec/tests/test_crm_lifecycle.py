"""
Mid-call, disconnect and shared-grant behaviour of a CRM connection, driven
through execute_chain and the real routes. Real Postgres; the CRM and the
provider's token/revoke endpoints are MockTransports that record every request.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks

from libs.config_sdk.secrets import decrypt_tenant_secret, encrypt_tenant_secret
from libs.tenancy import set_target_tenant
from services.config import auth
from services.toolexec import custom_apis, executor, oauth
from services.toolexec.executor import ChainExecuteRequest  # noqa: F401  (documented executor surface)

from .test_chain_execution import _mock, _request
from .test_executor_crm import (  # noqa: F401  (fixtures are used by name)
    CALL, Upstream, _client, _fake_dns, _step_row, admin_headers, apply_crm, crm_cleanup, seed_connection, zoho_body,
)

ACCOUNTS = "https://accounts.zoho.eu"


@pytest.fixture(autouse=True)
def _zoho_platform(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_OAUTH_REDIRECT_URI", "https://console.test/integrations/callback")
    monkeypatch.setenv("TOOLEXEC_OAUTH_ZOHO_CLIENT_ID", "zoho-client-id")
    monkeypatch.setenv("TOOLEXEC_OAUTH_ZOHO_CLIENT_SECRET_REF", "env:TOOLEXEC_TEST_ZOHO_SECRET")
    monkeypatch.setenv("TOOLEXEC_TEST_ZOHO_SECRET", "zoho-client-secret")


class Provider:
    """The identity provider: answers token and revoke calls, recording each."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_status, self.token_body = 200, None
        self.delay = 0.0
        self.issued = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/token"):
            await asyncio.sleep(self.delay)
            self.issued += 1
            body = self.token_body if self.token_body is not None else {
                "access_token": f"at-new-{self.issued}", "expires_in": 3600, "refresh_token": f"rt-new-{self.issued}",
            }
            return httpx.Response(self.token_status, json=body)
        return httpx.Response(200, json={})

    def calls(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]


@pytest.fixture
def provider(monkeypatch) -> Provider:
    recorder = Provider()
    monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(recorder))
    return recorder


async def _setup(pool, tenant, agent, monkeypatch, upstream, **seed) -> str:
    connection = await seed_connection(pool, tenant, "zoho", **seed)
    await pool.execute("UPDATE oauth_connections SET accounts_server = $2 WHERE id = $1",
                       uuid.UUID(connection), ACCOUNTS)
    await apply_crm(tenant, "zoho", enable_for=agent)
    monkeypatch.setattr(executor, "_step_transport", _mock(upstream))
    return connection


async def _lookup(tenant, agent, **overrides):
    return await executor.execute_chain(_request(tenant, agent, "crm_lookup_contact", **CALL, **overrides))


async def _connection(pool, connection: str) -> dict:
    return dict(await pool.fetchrow("SELECT * FROM oauth_connections WHERE id = $1", uuid.UUID(connection)))


# ── mid-call token handling ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_expired_access_token_completes_after_exactly_one_refresh(
    pool, tenant_agent, crm_cleanup, provider, monkeypatch,
):
    tenant, agent = tenant_agent
    upstream = Upstream(body=zoho_body())
    await _setup(pool, tenant, agent, monkeypatch, upstream, expires_in_s=-30)

    first, second = await _lookup(tenant, agent), await _lookup(tenant, agent)

    assert first.chain_status == second.chain_status == "success"
    assert len(provider.calls("/token")) == 1
    assert [r.headers["authorization"] for r in upstream.requests] == ["Bearer at-new-1"] * 2
    form = provider.calls("/token")[0].content.decode()
    assert "grant_type=refresh_token" in form and "client_secret=zoho-client-secret" in form
    assert "client_secret" not in str(provider.calls("/token")[0].url)  # the secret is in the body, never the URL


@pytest.mark.asyncio
async def test_invalid_grant_flips_to_reconnect_needed_and_the_step_fails_fast(
    pool, tenant_agent, crm_cleanup, provider, monkeypatch, admin_headers,
):
    tenant, agent = tenant_agent
    upstream = Upstream(body=zoho_body())
    connection = await _setup(pool, tenant, agent, monkeypatch, upstream, expires_in_s=-30)
    provider.token_status, provider.token_body = 400, {"error": "invalid_grant"}

    started = time.monotonic()
    response = await _lookup(tenant, agent)

    assert time.monotonic() - started < 6  # the per-step timeout
    assert (response.chain_status, response.error) == ("unavailable", "reconnect_required")
    assert upstream.requests == []
    row = await _connection(pool, connection)
    assert row["status"] == "reconnect_needed" and row["refresh_token_ref"] is None
    async with _client() as c:
        listed = await c.get(f"/tenants/{tenant['id']}/oauth-connections", headers=admin_headers)
    assert [(x["id"], x["status"]) for x in listed.json()] == [(connection, "reconnect_needed")]


@pytest.mark.asyncio
async def test_a_zoho_grant_with_no_stored_api_origin_lists_as_reconnect_needed(
    pool, tenant_agent, crm_cleanup, admin_headers,
):
    tenant, _agent = tenant_agent
    connection = await seed_connection(pool, tenant, "zoho", api_base_url=None)

    async with _client() as c:
        listed = await c.get(f"/tenants/{tenant['id']}/oauth-connections", headers=admin_headers)

    assert [(x["id"], x["status"]) for x in listed.json()] == [(connection, "reconnect_needed")]
    assert "api_base_url" not in listed.json()[0]
    assert (await _connection(pool, connection))["status"] == "connected"


@pytest.mark.asyncio
async def test_a_5xx_or_slow_crm_gives_a_bounded_failure_and_leaves_the_connection_alone(
    pool, tenant_agent, crm_cleanup, provider, monkeypatch,
):
    tenant, agent = tenant_agent
    connection = await _setup(pool, tenant, agent, monkeypatch, Upstream(status=503, body={"x": 1}))
    before = await _connection(pool, connection)

    down = await _lookup(tenant, agent)
    assert (down.chain_status, down.error) == ("failed", "http_status_503")

    async def _slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=zoho_body())

    monkeypatch.setattr(executor, "_step_transport", _mock(_slow))
    started = time.monotonic()
    slow = await _lookup(tenant, agent, chain_budget_ms=800)  # the call's own budget, not a tuned constant
    assert time.monotonic() - started < 3
    assert (slow.chain_status, slow.error) == ("timeout", "step_timeout")

    after = await _connection(pool, connection)
    assert (after["status"], after["access_token_ref"], after["refresh_token_ref"]) == (
        before["status"], before["access_token_ref"], before["refresh_token_ref"])


@pytest.mark.asyncio
async def test_two_concurrent_refreshes_leave_one_winner_and_the_row_keeps_a_refresh_token(
    pool, tenant_agent, crm_cleanup, provider, monkeypatch,
):
    tenant, agent = tenant_agent
    upstream = Upstream(body=zoho_body())
    connection = await _setup(pool, tenant, agent, monkeypatch, upstream, expires_in_s=-30)
    provider.delay = 0.2  # both read the expired row before either has written

    first, second = await asyncio.gather(_lookup(tenant, agent), _lookup(tenant, agent))

    assert first.chain_status == second.chain_status == "success"
    assert len(provider.calls("/token")) == 2
    row = await _connection(pool, connection)
    stored_access = decrypt_tenant_secret(str(tenant["id"]), row["access_token_ref"])
    stored_refresh = decrypt_tenant_secret(str(tenant["id"]), row["refresh_token_ref"])
    assert stored_refresh in {"rt-new-1", "rt-new-2"}                    # never lost, never the spent one
    assert stored_access == f"at-new-{stored_refresh[-1]}"               # one winner's pair, not a mix
    assert {r.headers["authorization"] for r in upstream.requests} == {f"Bearer {stored_access}"}  # loser used it


# ── disconnect ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disconnect_returns_a_constant_clears_both_refs_and_the_next_lookup_needs_a_reconnect(
    pool, tenant_agent, crm_cleanup, provider, monkeypatch, admin_headers,
):
    tenant, agent = tenant_agent
    upstream = Upstream(body=zoho_body())
    connection = await _setup(pool, tenant, agent, monkeypatch, upstream)
    assert (await _lookup(tenant, agent)).chain_status == "success"

    async with _client() as c:
        r = await c.delete(f"/tenants/{tenant['id']}/oauth-connections/{connection}", headers=admin_headers)
    assert (r.status_code, r.json()) == (200, {"disconnected": True})
    row = await _connection(pool, connection)
    assert (row["status"], row["access_token_ref"], row["refresh_token_ref"]) == ("disconnected", None, None)

    upstream.requests.clear()
    after = await _lookup(tenant, agent)
    assert (after.chain_status, after.error) == ("unavailable", "reconnect_required")
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_the_upstream_revoke_runs_in_the_background_callback_not_the_response(
    pool, tenant_agent, crm_cleanup, provider, monkeypatch,
):
    tenant, agent = tenant_agent
    connection = await _setup(pool, tenant, agent, monkeypatch, Upstream())
    background = BackgroundTasks()

    body = await oauth.disconnect(
        tenant_id=str(tenant["id"]), connection_id=connection, user_id=None, user_email=None,
        background_tasks=background,
    )

    assert body == {"disconnected": True}
    assert len(background.tasks) == 1 and background.tasks[0].func is oauth._revoke_upstream
    assert provider.requests == []               # nothing has left the process yet
    await background()
    (revoke,) = provider.requests
    assert str(revoke.url) == f"{ACCOUNTS}/oauth/v2/token/revoke"
    assert revoke.content.decode() == "token=rt-1"


@pytest.mark.asyncio
async def test_a_foreign_connection_id_answers_exactly_like_an_absent_one_for_the_same_principal(
    pool, tenant_agent, crm_cleanup, tenant_b, admin_headers,
):
    tenant, _agent = tenant_agent
    foreign = await seed_connection(pool, tenant_b, "zoho")
    async with _client() as c:
        base = f"/tenants/{tenant['id']}/oauth-connections"
        absent = await c.delete(f"{base}/{uuid.uuid4()}", headers=admin_headers)
        other = await c.delete(f"{base}/{foreign}", headers=admin_headers)
    assert absent.status_code == other.status_code == 404
    assert absent.content == other.content
    assert (await _connection(pool, foreign))["status"] == "connected"  # and nothing of B's moved


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_b(pool):
    slug = f"crm-b-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING *", slug))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"]))
    yield tenant
    set_target_tenant(None)
    await pool.execute("DELETE FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM api_chain_steps WHERE run_id IN "
                       "(SELECT id FROM api_chain_runs WHERE tenant_id = $1)", tenant["id"])
    await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM custom_api_params WHERE custom_api_id IN "
                       "(SELECT id FROM custom_apis WHERE tenant_id = $1)", tenant["id"])
    await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest.mark.asyncio
async def test_a_shared_grant_is_not_revoked_until_the_last_holder_goes(
    pool, tenant_agent, crm_cleanup, tenant_b, provider, monkeypatch, admin_headers,
):
    tenant, agent = tenant_agent
    upstream = Upstream(body=zoho_body())
    a_conn = await _setup(pool, tenant, agent, monkeypatch, upstream)
    b_conn = await seed_connection(pool, tenant_b, "zoho")
    await pool.execute("UPDATE oauth_connections SET provider_sub = 'ZUID-1', accounts_server = $2 "
                       "WHERE id = ANY($1::uuid[])", [uuid.UUID(a_conn), uuid.UUID(b_conn)], ACCOUNTS)
    path = f"/tenants/{tenant['id']}/oauth-connections/{a_conn}"

    async with _client() as c:
        shared = await c.delete(path, headers=admin_headers)
        assert provider.calls("/revoke") == []                       # B still holds the grant: nothing revoked
        assert (await _connection(pool, b_conn))["status"] == "connected"
        b_token, _, _, _ = await oauth.access_token_for(str(tenant_b["id"]), b_conn)
        assert b_token == "at-1"                                     # B's next lookup still has its token

        # A reconnects, B's row is deleted, and the same principal disconnects again.
        await pool.execute(
            "UPDATE oauth_connections SET status = 'connected', access_token_ref = $2, refresh_token_ref = $3, "
            "access_expires_at = now() + interval '1 hour' WHERE id = $1", uuid.UUID(a_conn),
            encrypt_tenant_secret(str(tenant["id"]), "at-1"), encrypt_tenant_secret(str(tenant["id"]), "rt-1"))
        await pool.execute("UPDATE oauth_connections SET deleted_at = now(), status = 'disconnected', "
                           "access_token_ref = NULL, refresh_token_ref = NULL, access_expires_at = NULL "
                           "WHERE id = $1", uuid.UUID(b_conn))
        last = await c.delete(path, headers=admin_headers)

    assert (shared.status_code, shared.content) == (last.status_code, last.content)  # one principal, same answer
    assert len(provider.calls("/revoke")) == 1 and provider.calls("/revoke")[0].content == b"token=rt-1"


# ── role gate ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_viewer_is_refused_connect_disconnect_and_api_key(pool, tenant_agent, crm_cleanup):
    tenant, _agent = tenant_agent
    row = await pool.fetchrow(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES ($1, $2, 'x', 'viewer') "
        "RETURNING id, email", tenant["id"], f"viewer-{uuid.uuid4().hex[:8]}@crm.test")
    token = auth.create_access_token({"id": str(row["id"]), "email": row["email"], "role": "viewer",
                                      "tenant_id": str(tenant["id"]), "is_service_account": False})
    headers = {"Authorization": f"Bearer {token}"}
    base = f"/tenants/{tenant['id']}/oauth-connections"
    try:
        async with _client() as c:
            codes = [
                (await c.post(f"{base}/salesforce/authorize", headers=headers, json={})).status_code,
                (await c.post(f"{base}/callback", headers=headers, json={"state": "s", "code": "c"})).status_code,
                (await c.delete(f"{base}/{uuid.uuid4()}", headers=headers)).status_code,
                (await c.post(f"{base}/calcom/api-key", headers=headers, json={"api_key": "k"})).status_code,
            ]
    finally:
        await pool.execute("DELETE FROM users WHERE id = $1", row["id"])
    assert codes == [403, 403, 403, 403]
