"""
Route-level auth/tenant-isolation tests for services/toolexec/app.py
(T16-T19) — real Postgres (tenant_agent/pool fixtures), a real signed JWT
per role (services.config.auth.create_access_token — the same module
services/config/tests/test_console_gate.py already proves is shared
across services, lesson 9), and httpx's ASGITransport against the real
app (no mocked routing).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from services.config import auth
from services.toolexec import custom_apis, executor
from services.toolexec.app import app


def _user_dict(role: str, tenant_id: str | None, *, is_service_account: bool = False,
               email: str = "x@example.com") -> dict:
    return {
        "id": str(uuid.uuid4()), "email": email, "role": role, "tenant_id": tenant_id,
        "is_service_account": is_service_account,
    }


def _bearer(role: str, tenant_id: str | None, **kwargs) -> dict:
    token = auth.create_access_token(_user_dict(role, tenant_id, **kwargs))
    return {"Authorization": f"Bearer {token}"}


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)


async def _make_tenant(pool, slug: str) -> dict:
    return dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"T {slug}", slug,
    ))


async def _make_agent(pool, tenant_id) -> dict:
    return dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant_id,
    ))


# ── T16 — admin surface: read/write split ─────────────────────────────────

@pytest.mark.asyncio
async def test_viewer_403s_on_every_write_route_and_200s_on_reads(pool, tenant_agent):
    tenant, _agent = tenant_agent
    api = await custom_apis.create_custom_api(
        tenant_id=str(tenant["id"]), name=f"api_{uuid.uuid4().hex[:8]}", description="d",
        endpoint_url="https://example.com/api", method="GET",
    )
    viewer_headers = _bearer("viewer", str(tenant["id"]))

    async with _client() as c:
        # Writes: 403.
        r = await c.post(f"/tenants/{tenant['id']}/custom-apis", headers=viewer_headers, json={
            "name": "x", "description": "d", "endpoint_url": "https://example.com/x", "method": "GET",
        })
        assert r.status_code == 403
        r = await c.patch(f"/custom-apis/{api['id']}", headers=viewer_headers, json={"description": "new"})
        assert r.status_code == 403
        r = await c.delete(f"/custom-apis/{api['id']}", headers=viewer_headers)
        assert r.status_code == 403

        # Reads: 200 with the expected body (reads are not locked out —
        # the task cannot be satisfied by 403ing everything).
        r = await c.get(f"/tenants/{tenant['id']}/custom-apis", headers=viewer_headers)
        assert r.status_code == 200
        assert any(row["id"] == str(api["id"]) for row in r.json())

        r = await c.get(f"/custom-apis/{api['id']}", headers=viewer_headers)
        assert r.status_code == 200
        assert r.json()["id"] == str(api["id"])
        assert r.json()["name"] == api["name"]


@pytest.mark.asyncio
async def test_admin_soft_delete_conflict_returns_409(pool, tenant_agent):
    tenant, _agent = tenant_agent
    leaf = await custom_apis.create_custom_api(
        tenant_id=str(tenant["id"]), name=f"leaf_{uuid.uuid4().hex[:8]}", description="d",
        endpoint_url="https://example.com/leaf", method="GET",
    )
    await custom_apis.create_custom_api(
        tenant_id=str(tenant["id"]), name=f"root_{uuid.uuid4().hex[:8]}", description="d",
        endpoint_url="https://example.com/root", method="GET",
        params=[{
            "name": "a", "location": "query", "json_type": "string", "required": True,
            "source": "upstream", "upstream_api_id": leaf["id"], "upstream_json_path": "$.id",
        }],
    )
    admin_headers = _bearer("admin", str(tenant["id"]))

    async with _client() as c:
        r = await c.delete(f"/custom-apis/{leaf['id']}", headers=admin_headers)
        assert r.status_code == 409


# ── T17 — agent enablement: AC-10 write-time gate ─────────────────────────

@pytest.mark.asyncio
async def test_put_cross_tenant_custom_api_id_404s_byte_identical_no_row(pool, tenant_agent):
    tenant_a, agent_a = tenant_agent
    other_tenant = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:8]}")
    try:
        other_api = await custom_apis.create_custom_api(
            tenant_id=str(other_tenant["id"]), name=f"api_{uuid.uuid4().hex[:8]}", description="d",
            endpoint_url="https://example.com/api", method="GET",
        )
        admin_headers = _bearer("admin", str(tenant_a["id"]))
        random_id = str(uuid.uuid4())

        async with _client() as c:
            cross_tenant_resp = await c.put(
                f"/agents/{agent_a['id']}/custom-apis/{other_api['id']}",
                headers=admin_headers, json={"enabled": True},
            )
            random_uuid_resp = await c.put(
                f"/agents/{agent_a['id']}/custom-apis/{random_id}",
                headers=admin_headers, json={"enabled": True},
            )
            assert cross_tenant_resp.status_code == 404
            assert random_uuid_resp.status_code == 404
            assert cross_tenant_resp.json()["detail"] == random_uuid_resp.json()["detail"]

            # DELETE: same shape.
            cross_tenant_del = await c.delete(
                f"/agents/{agent_a['id']}/custom-apis/{other_api['id']}", headers=admin_headers,
            )
            random_uuid_del = await c.delete(
                f"/agents/{agent_a['id']}/custom-apis/{random_id}", headers=admin_headers,
            )
            assert cross_tenant_del.status_code == 404
            assert random_uuid_del.status_code == 404
            assert cross_tenant_del.json()["detail"] == random_uuid_del.json()["detail"]

        rows = await pool.fetch("SELECT * FROM agent_custom_apis WHERE agent_id = $1", agent_a["id"])
        assert rows == []  # the guard runs BEFORE any INSERT/UPDATE, not after
    finally:
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


@pytest.mark.asyncio
async def test_put_tenant_bs_agent_id_404s_byte_identical_no_row(pool, tenant_agent):
    tenant_a, _agent_a = tenant_agent
    other_tenant = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:8]}")
    try:
        other_agent = await _make_agent(pool, other_tenant["id"])
        own_api = await custom_apis.create_custom_api(
            tenant_id=str(tenant_a["id"]), name=f"api_{uuid.uuid4().hex[:8]}", description="d",
            endpoint_url="https://example.com/api", method="GET",
        )
        admin_headers = _bearer("admin", str(tenant_a["id"]))
        random_id = str(uuid.uuid4())

        async with _client() as c:
            wrong_agent_resp = await c.put(
                f"/agents/{other_agent['id']}/custom-apis/{own_api['id']}",
                headers=admin_headers, json={"enabled": True},
            )
            random_uuid_resp = await c.put(
                f"/agents/{random_id}/custom-apis/{own_api['id']}",
                headers=admin_headers, json={"enabled": True},
            )
            assert wrong_agent_resp.status_code == 404
            assert random_uuid_resp.status_code == 404
            assert wrong_agent_resp.json()["detail"] == random_uuid_resp.json()["detail"]

        rows = await pool.fetch("SELECT * FROM agent_custom_apis WHERE agent_id = $1", other_agent["id"])
        assert rows == []
    finally:
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


@pytest.mark.asyncio
async def test_put_wrong_tenant_caller_404s_byte_identical_no_row(pool, tenant_agent):
    """Distinct from both cases above: here the agent AND the custom_api
    genuinely belong to the SAME tenant (tenant B) — the query's own JOIN
    condition (ca.tenant_id = a.tenant_id) is satisfied, so it alone
    cannot reject this. Only the caller-tenant boundary check
    (current_user.tenant_id vs the row's tenant_id) can. This is the
    shape that silently passed against a mutation removing that check in
    an earlier verification pass, because the OTHER two route tests both
    happen to fail the JOIN condition first — this one does not."""
    tenant_a, _agent_a = tenant_agent
    other_tenant = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:8]}")
    try:
        other_agent = await _make_agent(pool, other_tenant["id"])
        other_api = await custom_apis.create_custom_api(
            tenant_id=str(other_tenant["id"]), name=f"api_{uuid.uuid4().hex[:8]}", description="d",
            endpoint_url="https://example.com/api", method="GET",
        )
        wrong_tenant_admin = _bearer("admin", str(tenant_a["id"]))
        random_id = str(uuid.uuid4())

        async with _client() as c:
            wrong_tenant_resp = await c.put(
                f"/agents/{other_agent['id']}/custom-apis/{other_api['id']}",
                headers=wrong_tenant_admin, json={"enabled": True},
            )
            random_uuid_resp = await c.put(
                f"/agents/{random_id}/custom-apis/{random_id}",
                headers=wrong_tenant_admin, json={"enabled": True},
            )
            assert wrong_tenant_resp.status_code == 404
            assert random_uuid_resp.status_code == 404
            assert wrong_tenant_resp.json()["detail"] == random_uuid_resp.json()["detail"]

        rows = await pool.fetch("SELECT * FROM agent_custom_apis WHERE agent_id = $1", other_agent["id"])
        assert rows == []
    finally:
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


# ── T18 — chain-history read ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_history_cross_tenant_session_404s_platform_scoped_sees_rows(pool, tenant_agent):
    tenant_a, agent_a = tenant_agent
    other_tenant = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:8]}")
    try:
        other_agent = await _make_agent(pool, other_tenant["id"])
        other_api = await custom_apis.create_custom_api(
            tenant_id=str(other_tenant["id"]), name=f"api_{uuid.uuid4().hex[:8]}", description="d",
            endpoint_url="https://example.com/api", method="GET",
        )
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        run = dict(await pool.fetchrow(
            "INSERT INTO api_chain_runs (tenant_id, agent_id, session_id, tool_call_id, idempotency_key, "
            "target_api_id) VALUES ($1, $2, $3, 'tc', 'idem', $4) RETURNING *",
            other_tenant["id"], other_agent["id"], session_id, other_api["id"],
        ))

        tenant_a_admin = _bearer("admin", str(tenant_a["id"]))
        platform_caller = _bearer("superadmin", None)
        unknown_session = f"sess-{uuid.uuid4().hex[:8]}"

        async with _client() as c:
            cross_tenant_resp = await c.get(f"/calls/{session_id}/chain-runs", headers=tenant_a_admin)
            unknown_resp = await c.get(f"/calls/{unknown_session}/chain-runs", headers=tenant_a_admin)
            assert cross_tenant_resp.status_code == 404
            assert unknown_resp.status_code == 404
            assert cross_tenant_resp.json()["detail"] == unknown_resp.json()["detail"]

            platform_resp = await c.get(f"/calls/{session_id}/chain-runs", headers=platform_caller)
            assert platform_resp.status_code == 200
            assert len(platform_resp.json()) == 1
            assert platform_resp.json()[0]["id"] == str(run["id"])
    finally:
        await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


# ── T19 — execute-identity gate ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_route_401s_with_no_auth_header(pool, tenant_agent):
    tenant, agent = tenant_agent
    async with _client() as c:
        assert (await c.get(f"/tenants/{tenant['id']}/custom-apis")).status_code == 401
        assert (await c.get("/custom-apis/x")).status_code == 401
        assert (await c.get(f"/agents/{agent['id']}/custom-apis")).status_code == 401
        assert (await c.get("/calls/x/chain-runs")).status_code == 401
        assert (await c.post("/internal/chains/execute", json={})).status_code == 401
        assert (await c.get("/health")).status_code == 200  # public route still works (lesson 1)


@pytest.mark.asyncio
async def test_viewer_service_account_not_in_allowlist_403s():
    """The vobiz/SDK case: a role=viewer, tenant_id=NULL service account
    NOT on TOOLEXEC_EXECUTE_SUBJECTS. This is what fails under an
    is_platform_scoped-only gate, since tenant_id=NULL alone would pass it."""
    headers = _bearer("viewer", None, is_service_account=True, email="vobiz-service@internal.yuviz.ai")
    async with _client() as c:
        r = await c.post("/internal/chains/execute", headers=headers, json={
            "tenant_id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "call_id": "c",
            "session_id": "s", "turn_id": "t", "tool_call_id": "tc", "idempotency_key": "idem",
            "api_name": "x", "chain_budget_ms": 5000, "max_chain_depth": 4,
        })
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_human_superadmin_403s_proving_is_service_account_is_load_bearing():
    """A human superadmin whose email happens to equal the allow-listed
    identity string still 403s, because is_service_account is False —
    proving the gate checks BOTH conditions, not just the email."""
    headers = _bearer("superadmin", None, is_service_account=False,
                       email="conversation-service@internal.yuviz.ai")
    async with _client() as c:
        r = await c.post("/internal/chains/execute", headers=headers, json={
            "tenant_id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "call_id": "c",
            "session_id": "s", "turn_id": "t", "tool_call_id": "tc", "idempotency_key": "idem",
            "api_name": "x", "chain_budget_ms": 5000, "max_chain_depth": 4,
        })
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_allowlisted_conversation_account_succeeds(pool, tenant_agent, monkeypatch):
    tenant, agent = tenant_agent
    api = await custom_apis.create_custom_api(
        tenant_id=str(tenant["id"]), name=f"execok_{uuid.uuid4().hex[:8]}", description="d",
        endpoint_url="https://example.com/api", method="GET",
    )
    from services.config.auth import CurrentUser as _CU
    from services.toolexec import agent_apis as agent_apis_service
    await agent_apis_service.set_enabled(
        agent["id"], api["id"], enabled=True,
        current_user=_CU(id=str(uuid.uuid4()), email="a@test", role="admin", tenant_id=str(tenant["id"])),
    )

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(executor, "_step_transport", lambda allowed_ips: httpx.MockTransport(handler))

    headers = _bearer("viewer", None, is_service_account=True,
                       email="conversation-service@internal.yuviz.ai")
    async with _client() as c:
        r = await c.post("/internal/chains/execute", headers=headers, json={
            "tenant_id": str(tenant["id"]), "agent_id": str(agent["id"]), "call_id": "c",
            "session_id": "s", "turn_id": "t", "tool_call_id": f"tc-{uuid.uuid4().hex[:8]}",
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}", "api_name": api["name"],
            "chain_budget_ms": 5000, "max_chain_depth": 4,
        })
    assert r.status_code == 200
    assert r.json()["chain_status"] == "success"


# ── FIX 4a/4b — the blanket exception handlers must not leak infrastructure
# facts, verified through a REAL live request, not just a handler unit test ──

@pytest.mark.asyncio
async def test_ssrf_rejection_over_http_never_returns_the_resolved_ip(pool, tenant_agent, monkeypatch, caplog):
    tenant, _agent = tenant_agent
    denied_ip = "10.0.3.7"

    async def _resolver(hostname, port):
        return [denied_ip]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)

    admin_headers = _bearer("admin", str(tenant["id"]))
    async with _client() as c:
        with caplog.at_level("WARNING"):
            r = await c.post(
                f"/tenants/{tenant['id']}/custom-apis", headers=admin_headers,
                json={
                    "name": f"ssrf_{uuid.uuid4().hex[:8]}", "description": "d",
                    "endpoint_url": "https://internal-billing.corp/", "method": "GET",
                },
            )

    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail == "invalid_endpoint_url: resolves to a denied address"
    assert denied_ip not in detail
    assert "internal-billing.corp" not in detail
    # The rejected reason IS observable server-side — this proves the
    # information still exists for an operator to debug, it just never
    # crosses the HTTP response.
    joined_log = " ".join(rec.getMessage() for rec in caplog.records)
    assert denied_ip in joined_log
    assert "internal-billing.corp" in joined_log


@pytest.mark.asyncio
async def test_lookup_error_over_http_never_leaks_the_underlying_message(pool, tenant_agent, monkeypatch, caplog):
    """Simulates the exact shape finding 1 describes — a raw KeyError from
    a secret resolver, which IS a LookupError subclass, embedding a
    credential ref and an absolute mount path — reaching the blanket
    LookupError handler through a real live route."""
    sentinel_leak = "K8sFileResolver: no secret file at /var/run/tenant-secrets/tenants/t1/probe (ref='k8s:tenants/t1/probe')"

    async def _boom(custom_api_id):
        raise KeyError(sentinel_leak)

    monkeypatch.setattr(custom_apis, "get_custom_api", _boom)

    tenant, _agent = tenant_agent
    admin_headers = _bearer("admin", str(tenant["id"]))
    async with _client() as c:
        with caplog.at_level("INFO"):
            r = await c.get(f"/custom-apis/{uuid.uuid4()}", headers=admin_headers)

    assert r.status_code == 404
    assert r.json()["detail"] == "not found"
    assert sentinel_leak not in r.text
    joined_log = " ".join(rec.getMessage() for rec in caplog.records)
    assert sentinel_leak in joined_log
