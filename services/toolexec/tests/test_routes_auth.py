"""Route-level auth/tenant-isolation tests against the real app, Postgres and signed JWTs."""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import auth
from services.toolexec import custom_apis, executor
from services.toolexec import db as toolexec_db
from services.toolexec.routers import execute as execute_router
from services.toolexec.app import app


_created_user_ids: list = []


async def _bearer(role: str, tenant_id: str | None, *, is_service_account: bool = False,
                  email: str | None = None) -> dict:
    """Token for a real users row: Config's auth re-reads users on every request."""
    email = email or f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    pool = await toolexec_db.get_pool()
    user_id = await pool.fetchval(
        "INSERT INTO users (email, role, tenant_id, is_service_account, password_hash) "
        "VALUES ($1, $2, $3, $4, 'not-a-real-hash') RETURNING id",
        email, role, tenant_id, is_service_account,
    )
    _created_user_ids.append(user_id)
    token = auth.create_access_token({
        "id": str(user_id), "email": email, "role": role, "tenant_id": tenant_id,
        "is_service_account": is_service_account,
    })
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _retire_test_users():
    yield
    if _created_user_ids:
        pool = await toolexec_db.get_pool()
        # Soft delete: audit_log rows may reference these users.
        await pool.execute("UPDATE users SET deleted_at = now() WHERE id = ANY($1::uuid[])", _created_user_ids)
        _created_user_ids.clear()


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
    viewer_headers = await _bearer("viewer", str(tenant["id"]))

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

        # Reads: 200.
        r = await c.get(f"/tenants/{tenant['id']}/custom-apis", headers=viewer_headers)
        assert r.status_code == 200
        assert any(row["id"] == str(api["id"]) for row in r.json())

        r = await c.get(f"/custom-apis/{api['id']}", headers=viewer_headers)
        assert r.status_code == 200
        assert r.json()["id"] == str(api["id"])
        assert r.json()["name"] == api["name"]


@pytest.mark.asyncio
async def test_sensitive_literal_param_value_not_readable_by_every_tenant_role(pool, tenant_agent):
    """A viewer never sees a sensitive literal param's raw value on list or get."""
    secret_value = "sk-live-do-not-leak-me"
    api = await custom_apis.create_custom_api(
        tenant_id=str(tenant_agent[0]["id"]), name=f"secretapi_{uuid.uuid4().hex[:8]}", description="d",
        endpoint_url="https://example.com/api", method="POST", side_effecting=True,
        params=[{
            "name": "X-Api-Key", "location": "header", "json_type": "string",
            "required": True, "source": "literal", "literal_value": secret_value, "sensitive": True,
        }],
    )
    viewer_headers = await _bearer("viewer", str(tenant_agent[0]["id"]))

    async with _client() as c:
        r_list = await c.get(f"/tenants/{tenant_agent[0]['id']}/custom-apis", headers=viewer_headers)
        assert r_list.status_code == 200
        listed = next(row for row in r_list.json() if row["id"] == str(api["id"]))
        listed_param = next(p for p in listed["params"] if p["name"] == "X-Api-Key")
        assert listed_param["literal_value"] != secret_value

        r_get = await c.get(f"/custom-apis/{api['id']}", headers=viewer_headers)
        assert r_get.status_code == 200
        got_param = next(p for p in r_get.json()["params"] if p["name"] == "X-Api-Key")
        assert got_param["literal_value"] != secret_value


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
    admin_headers = await _bearer("admin", str(tenant["id"]))

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
        admin_headers = await _bearer("admin", str(tenant_a["id"]))
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
        await pool.execute("UPDATE users SET tenant_id = NULL, deleted_at = coalesce(deleted_at, now()) WHERE tenant_id = $1", other_tenant["id"])
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
        admin_headers = await _bearer("admin", str(tenant_a["id"]))
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
        await pool.execute("UPDATE users SET tenant_id = NULL, deleted_at = coalesce(deleted_at, now()) WHERE tenant_id = $1", other_tenant["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


@pytest.mark.asyncio
async def test_put_wrong_tenant_caller_404s_byte_identical_no_row(pool, tenant_agent):
    """Agent and api both in tenant B: only the caller-tenant check (not the JOIN) rejects."""
    tenant_a, _agent_a = tenant_agent
    other_tenant = await _make_tenant(pool, f"other-{uuid.uuid4().hex[:8]}")
    try:
        other_agent = await _make_agent(pool, other_tenant["id"])
        other_api = await custom_apis.create_custom_api(
            tenant_id=str(other_tenant["id"]), name=f"api_{uuid.uuid4().hex[:8]}", description="d",
            endpoint_url="https://example.com/api", method="GET",
        )
        wrong_tenant_admin = await _bearer("admin", str(tenant_a["id"]))
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
        await pool.execute("UPDATE users SET tenant_id = NULL, deleted_at = coalesce(deleted_at, now()) WHERE tenant_id = $1", other_tenant["id"])
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

        tenant_a_admin = await _bearer("admin", str(tenant_a["id"]))
        platform_caller = await _bearer("superadmin", None)
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
        await pool.execute("UPDATE users SET tenant_id = NULL, deleted_at = coalesce(deleted_at, now()) WHERE tenant_id = $1", other_tenant["id"])
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
    """A platform-scoped service account not on the allow-list is refused."""
    headers = await _bearer("viewer", None, is_service_account=True,
                            email=f"vobiz-service-{uuid.uuid4().hex[:8]}@internal.yuviz.ai")
    async with _client() as c:
        r = await c.post("/internal/chains/execute", headers=headers, json={
            "tenant_id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "call_id": "c",
            "session_id": "s", "turn_id": "t", "tool_call_id": "tc", "idempotency_key": "idem",
            "api_name": "x", "chain_budget_ms": 5000, "max_chain_depth": 4,
        })
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_human_superadmin_403s_proving_is_service_account_is_load_bearing(monkeypatch):
    """A human superadmin with an allow-listed email still 403s."""
    email = f"conversation-service-{uuid.uuid4().hex[:8]}@internal.yuviz.ai"
    monkeypatch.setattr(execute_router, "_EXECUTE_SUBJECTS", frozenset({email}))
    headers = await _bearer("superadmin", None, is_service_account=False, email=email)
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

    email = f"conversation-service-{uuid.uuid4().hex[:8]}@internal.yuviz.ai"
    monkeypatch.setattr(execute_router, "_EXECUTE_SUBJECTS", frozenset({email}))
    headers = await _bearer("viewer", None, is_service_account=True, email=email)
    async with _client() as c:
        r = await c.post("/internal/chains/execute", headers=headers, json={
            "tenant_id": str(tenant["id"]), "agent_id": str(agent["id"]), "call_id": "c",
            "session_id": "s", "turn_id": "t", "tool_call_id": f"tc-{uuid.uuid4().hex[:8]}",
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}", "api_name": api["name"],
            "chain_budget_ms": 5000, "max_chain_depth": 4,
        })
    assert r.status_code == 200
    assert r.json()["chain_status"] == "success"


# ── exception handlers must not leak infrastructure details ──────────────

@pytest.mark.asyncio
async def test_ssrf_rejection_over_http_never_returns_the_resolved_ip(pool, tenant_agent, monkeypatch, caplog):
    tenant, _agent = tenant_agent
    denied_ip = "10.0.3.7"

    async def _resolver(hostname, port):
        return [denied_ip]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)

    admin_headers = await _bearer("admin", str(tenant["id"]))
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
    # Still available to operators in the server log.
    joined_log = " ".join(rec.getMessage() for rec in caplog.records)
    assert denied_ip in joined_log
    assert "internal-billing.corp" in joined_log


@pytest.mark.asyncio
async def test_lookup_error_over_http_never_leaks_the_underlying_message(pool, tenant_agent, monkeypatch, caplog):
    """A resolver KeyError carrying a ref and mount path reaches the client as a bare 404."""
    sentinel_leak = "K8sFileResolver: no secret file at /var/run/tenant-secrets/tenants/t1/probe (ref='k8s:tenants/t1/probe')"

    async def _boom(custom_api_id, **_kwargs):
        raise KeyError(sentinel_leak)

    monkeypatch.setattr(custom_apis, "get_custom_api", _boom)

    tenant, _agent = tenant_agent
    admin_headers = await _bearer("admin", str(tenant["id"]))
    async with _client() as c:
        with caplog.at_level("INFO"):
            r = await c.get(f"/custom-apis/{uuid.uuid4()}", headers=admin_headers)

    assert r.status_code == 404
    assert r.json()["detail"] == "not found"
    assert sentinel_leak not in r.text
    joined_log = " ".join(rec.getMessage() for rec in caplog.records)
    assert sentinel_leak in joined_log
