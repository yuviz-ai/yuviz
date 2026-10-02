"""HTTP-layer tests via ASGITransport against real Postgres + Redis (lifespan not run)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import generate_key
from libs.tenancy import set_target_tenant
from services.config import auth
from services.config import users as users_service
from services.config.app import app


@pytest.fixture
async def client(test_superadmin):
    # Superadmin by default; auth itself is covered by TestAuthEndpoints.
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {test_superadmin['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture
async def anon_client():
    """No Authorization header at all — for asserting 401s."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def viewer_client(test_viewer):
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {test_viewer['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture
async def admin_client(test_admin):
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {test_admin['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


class TestTenantEndpoints:
    async def test_create_and_get_tenant(self, client):
        slug = f"test-{uuid.uuid4().hex[:8]}"
        try:
            resp = await client.post("/tenants", json={"name": "API Test", "slug": slug})
            assert resp.status_code == 201
            body = resp.json()
            assert body["slug"] == slug
            assert body["config_version"] == 1

            resp = await client.get(f"/tenants/{slug}")
            assert resp.status_code == 200
            assert resp.json()["slug"] == slug
        finally:
            from services.config import cache, db
            pool = await db.get_pool()
            await pool.execute("DELETE FROM tenants WHERE slug = $1", slug)
            await cache.invalidate(f"tenant:{slug}")

    async def test_get_unknown_tenant_returns_404(self, client):
        resp = await client.get("/tenants/does-not-exist")
        assert resp.status_code == 404

    async def test_update_tenant_via_patch(self, client, test_tenant):
        resp = await client.patch(f"/tenants/{test_tenant['id']}", json={"vad_hold_ms": 700})
        assert resp.status_code == 200
        assert resp.json()["vad_hold_ms"] == 700
        assert resp.json()["config_version"] == test_tenant["config_version"] + 1

    async def test_update_tenant_with_empty_body_is_400(self, client, test_tenant):
        resp = await client.patch(f"/tenants/{test_tenant['id']}", json={})
        assert resp.status_code == 400

    async def test_update_tenant_unknown_field_is_422(self, client, test_tenant):
        # Pydantic rejects an unrecognized field before it ever reaches
        # tenants_service.update_tenant()'s own ValueError check.
        resp = await client.patch(
            f"/tenants/{test_tenant['id']}", json={"not_a_real_field": "x"},
        )
        assert resp.status_code in (400, 422)

    async def test_delete_tenant(self, client, pool):
        create = await client.post(
            "/tenants", json={"name": "Delete Me", "slug": f"test-del-{uuid.uuid4().hex[:8]}"},
        )
        tenant = create.json()
        resp = await client.delete(f"/tenants/{tenant['id']}")
        assert resp.status_code == 204

        resp = await client.get(f"/tenants/{tenant['slug']}")
        assert resp.status_code == 404  # soft-deleted, excluded from reads

        await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])

    async def test_delete_tenant_with_active_agent_is_409(self, client, test_tenant):
        agent = await client.post(
            f"/tenants/{test_tenant['slug']}/agents", json={"slug": "support-agent", "name": "Support"},
        )
        assert agent.status_code == 201

        resp = await client.delete(f"/tenants/{test_tenant['id']}")
        assert resp.status_code == 409
        body = resp.json()
        assert body["active_agents"] == 1
        assert body["active_phone_numbers"] == 0

        # Untouched — still there, tenant not deleted.
        still_there = await client.get(f"/tenants/{test_tenant['slug']}")
        assert still_there.status_code == 200

    async def test_delete_tenant_with_active_phone_number_is_409(self, client, test_tenant, pool):
        did = f"test-did-{uuid.uuid4().hex[:8]}"
        await pool.execute(
            "INSERT INTO phone_numbers (tenant_id, did, status) VALUES ($1, $2, 'active')",
            test_tenant["id"], did,
        )
        try:
            resp = await client.delete(f"/tenants/{test_tenant['id']}")
            assert resp.status_code == 409
            body = resp.json()
            assert body["active_agents"] == 0
            assert body["active_phone_numbers"] == 1
        finally:
            await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)

    async def test_delete_tenant_force_true_bypasses_the_block(self, client, test_tenant):
        agent = await client.post(
            f"/tenants/{test_tenant['slug']}/agents", json={"slug": "support-agent", "name": "Support"},
        )
        assert agent.status_code == 201

        resp = await client.delete(f"/tenants/{test_tenant['id']}?force=true")
        assert resp.status_code == 204

        gone = await client.get(f"/tenants/{test_tenant['slug']}")
        assert gone.status_code == 404

    async def test_delete_tenant_retires_its_numbers_and_their_routes(self, client, test_tenant, pool):
        from services.config import cache, phone_numbers

        did = f"test-did-{uuid.uuid4().hex[:8]}"
        set_target_tenant(test_tenant["id"])
        await phone_numbers.create_phone_number(tenant_id=test_tenant["id"], did=did)
        assert await cache.get_json(f"did:{did}") is not None
        try:
            resp = await client.delete(f"/tenants/{test_tenant['id']}?force=true")
            assert resp.status_code == 204

            assert await pool.fetchval("SELECT deleted_at FROM phone_numbers WHERE did = $1", did) is not None
            assert await cache.get_json(f"did:{did}") is None
        finally:
            await pool.execute("DELETE FROM phone_numbers WHERE did = $1", did)

    async def test_delete_tenant_with_nothing_attached_needs_no_force(self, client, test_tenant):
        resp = await client.delete(f"/tenants/{test_tenant['id']}")
        assert resp.status_code == 204

    async def test_tenant_admin_sees_only_own_tenant_in_list(self, admin_client, test_tenant, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other List Tenant", f"test-other-list-{uuid.uuid4().hex[:8]}",
        )
        try:
            resp = await admin_client.get("/tenants")
            assert resp.status_code == 200
            slugs = {t["slug"] for t in resp.json()}
            assert slugs == {test_tenant["slug"]}
            assert other["slug"] not in slugs
        finally:
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_viewer_sees_only_own_tenant_in_list(self, viewer_client, test_tenant, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Viewer Tenant", f"test-other-viewer-{uuid.uuid4().hex[:8]}",
        )
        try:
            resp = await viewer_client.get("/tenants")
            assert resp.status_code == 200
            slugs = {t["slug"] for t in resp.json()}
            assert slugs == {test_tenant["slug"]}
        finally:
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_superadmin_still_sees_every_tenant_in_list(self, client, test_tenant, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Superadmin Tenant", f"test-other-super-{uuid.uuid4().hex[:8]}",
        )
        try:
            resp = await client.get("/tenants")
            assert resp.status_code == 200
            slugs = {t["slug"] for t in resp.json()}
            assert {test_tenant["slug"], other["slug"]} <= slugs
        finally:
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_client_supplied_tenant_filter_cannot_widen_a_tenant_admins_result(
        self, admin_client, test_tenant, pool,
    ):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Widen Attempt Tenant", f"test-widen-{uuid.uuid4().hex[:8]}",
        )
        try:
            # No ?tenant_id= is supported; a smuggled one must not widen the result.
            resp = await admin_client.get(f"/tenants?tenant_id={other['id']}")
            assert resp.status_code == 200
            slugs = {t["slug"] for t in resp.json()}
            assert slugs == {test_tenant["slug"]}
        finally:
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_get_tenant_by_slug_404s_for_a_different_tenants_admin(
        self, admin_client, pool,
    ):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Get Tenant", f"test-other-get-{uuid.uuid4().hex[:8]}",
        )
        try:
            known_other = await admin_client.get(f"/tenants/{other['slug']}")
            unknown = await admin_client.get("/tenants/does-not-exist-at-all")
            # Same status and detail template, so a foreign tenant isn't distinguishable from a missing one.
            assert known_other.status_code == unknown.status_code == 404
            assert known_other.json() == {"detail": f"tenant {other['slug']!r} not found"}
            assert unknown.json() == {"detail": "tenant 'does-not-exist-at-all' not found"}
        finally:
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_get_own_tenant_by_slug_still_works_for_tenant_admin(self, admin_client, test_tenant):
        resp = await admin_client.get(f"/tenants/{test_tenant['slug']}")
        assert resp.status_code == 200
        assert resp.json()["slug"] == test_tenant["slug"]


class TestTenantConcurrency:
    """PATCH /tenants/{id}/concurrency."""

    async def test_admin_can_patch_own_tenant(self, admin_client, test_tenant):
        resp = await admin_client.patch(
            f"/tenants/{test_tenant['id']}/concurrency", json={"max_concurrent_calls": 7},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["max_concurrent_calls"] == 7

    async def test_admin_patching_foreign_tenant_404s(self, admin_client, test_tenant, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Foreign", f"test-foreign-{uuid.uuid4().hex[:8]}",
        )
        try:
            resp = await admin_client.patch(
                f"/tenants/{other['id']}/concurrency", json={"max_concurrent_calls": 3},
            )
            assert resp.status_code == 404
            assert resp.json() == {"detail": f"tenant {str(other['id'])!r} not found"}
        finally:
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_superadmin_can_patch_any_tenant(self, client, test_tenant):
        resp = await client.patch(
            f"/tenants/{test_tenant['id']}/concurrency", json={"max_concurrent_calls": 9},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["max_concurrent_calls"] == 9

    async def test_viewer_403s(self, viewer_client, test_tenant):
        resp = await viewer_client.patch(
            f"/tenants/{test_tenant['id']}/concurrency", json={"max_concurrent_calls": 3},
        )
        assert resp.status_code == 403

    async def test_out_of_bounds_value_is_422(self, admin_client, test_tenant):
        resp = await admin_client.patch(
            f"/tenants/{test_tenant['id']}/concurrency", json={"max_concurrent_calls": 0},
        )
        assert resp.status_code == 422

    async def test_retenanted_admin_is_confined_to_the_fresh_tenant(
        self, test_tenant, pool,
    ):
        # Token still claims tenant A after the admin moves to B; the route must use the fresh row.
        other_tenant = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Tenant B", f"test-b-{uuid.uuid4().hex[:8]}",
        )
        admin_user = await users_service.create_user(
            email=f"test-retenanted-admin-{uuid.uuid4().hex[:8]}@example.com",
            password="test-password-not-real", role="admin", tenant_id=test_tenant["id"],
        )
        try:
            stale_token = auth.create_access_token(admin_user)  # still claims tenant A

            await users_service.update_user(admin_user["id"], tenant_id=other_tenant["id"])

            transport = ASGITransport(app=app)
            headers = {"Authorization": f"Bearer {stale_token}"}
            async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client_:
                foreign_now = await client_.patch(
                    f"/tenants/{test_tenant['id']}/concurrency", json={"max_concurrent_calls": 4},
                )
                assert foreign_now.status_code == 404

                own_now = await client_.patch(
                    f"/tenants/{other_tenant['id']}/concurrency", json={"max_concurrent_calls": 4},
                )
                assert own_now.status_code == 200, own_now.text
                assert own_now.json()["max_concurrent_calls"] == 4
        finally:
            await pool.execute(
                "UPDATE users SET deleted_at = now(), tenant_id = NULL WHERE id = $1", admin_user["id"],
            )
            await pool.execute("DELETE FROM tenants WHERE id = $1", other_tenant["id"])


class TestAgentEndpoints:
    async def test_create_and_get_agent(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "greeting": "Hi!"},
        )
        assert resp.status_code == 201
        created = resp.json()
        assert created["slug"] == "support-agent"
        assert "workflow" in created and "workflow_draft" not in created
        assert isinstance(created["workflow"], dict)

        resp = await client.get(f"/tenants/{test_tenant['slug']}/agents/support-agent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["greeting"] == "Hi!"
        assert "workflow" in body and "workflow_draft" not in body
        wf = await client.get(f"/tenants/{test_tenant['slug']}/agents/{created['id']}/workflow")
        assert wf.status_code == 200
        graph = wf.json()["workflow"]
        assert isinstance(graph, dict)
        start = next(n for n in graph["nodes"] if n["type"] == "start")
        assert start["data"]["greeting"] == "Hi!"
        assert body["workflow"] == graph

    async def test_creating_the_same_slug_twice_is_409_not_500(self, client, test_tenant):
        body = {"slug": "dupe-agent", "name": "Dupe"}
        assert (await client.post(f"/tenants/{test_tenant['slug']}/agents", json=body)).status_code == 201
        resp = await client.post(f"/tenants/{test_tenant['slug']}/agents", json=body)
        assert resp.status_code == 409
        assert "already taken" in resp.json()["detail"]

    async def test_create_agent_rejects_an_invalid_graph(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={
                "slug": "bad-graph", "name": "Bad",
                "workflow": {
                    "version": 1,
                    "nodes": [{"id": "n1", "type": "start", "position": {"x": 0, "y": 0},
                               "data": {"name": "greeting", "prompt": "Hi."}}],
                    "edges": [],
                },
            },
        )
        assert resp.status_code == 400
        assert any(e["id"] == "n1" for e in resp.json()["errors"])

    async def test_workflow_routes_reject_a_malformed_agent_id_with_400_not_500(
        self, client, test_tenant,
    ):
        base = f"/tenants/{test_tenant['slug']}/agents/not-a-uuid/workflow"
        assert (await client.get(base)).status_code == 400
        assert (await client.put(f"{base}/draft", json={"graph": {"version": 1, "nodes": [], "edges": []}})).status_code == 400
        assert (await client.post(f"{base}/validate", json={"graph": {"version": 1, "nodes": [], "edges": []}})).status_code == 400
        assert (await client.post(f"{base}/publish", json={})).status_code == 400
        assert (await client.get(f"{base}/versions")).status_code == 400
        assert (await client.get(f"{base}/versions/1")).status_code == 400
        assert (await client.post(f"{base}/versions/1/rollback")).status_code == 400

    async def test_admin_cannot_read_another_tenants_workflow(
        self, admin_client, test_tenant, pool,
    ):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other WF Tenant", f"other-wf-{uuid.uuid4().hex[:8]}",
        )
        try:
            from services.config import agents as agents_service
            set_target_tenant(str(other["id"]))
            try:
                victim = await agents_service.create_agent(
                    tenant_id=other["id"], slug="victim", name="Victim",
                    system_prompt="secret prompt IP",
                )
            finally:
                set_target_tenant(None)
            # Wrong-tenant slug is 404 (not 403) — same as a missing tenant.
            list_resp = await admin_client.get(f"/tenants/{other['slug']}/agents")
            assert list_resp.status_code == 404
            wf = await admin_client.get(
                f"/tenants/{other['slug']}/agents/{victim['id']}/workflow",
            )
            assert wf.status_code == 404
            publish = await admin_client.post(
                f"/tenants/{other['slug']}/agents/{victim['id']}/workflow/publish",
                json={"graph": {
                    "version": 1,
                    "nodes": [
                        {"id": "n1", "type": "start", "position": {"x": 0, "y": 0},
                         "data": {"name": "greeting", "prompt": "hijacked"}},
                        {"id": "n2", "type": "end", "position": {"x": 0, "y": 100},
                         "data": {"name": "bye", "prompt": "x", "disposition": "completed"}},
                    ],
                    "edges": [
                        {"id": "e1", "source": "n1", "target": "n2",
                         "data": {"label": "done", "condition": "Done."}},
                    ],
                }},
            )
            assert publish.status_code == 404
            own = await admin_client.get(f"/tenants/{test_tenant['slug']}/agents")
            assert own.status_code == 200
        finally:
            await pool.execute("DELETE FROM agents WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_null_tenant_admin_is_not_unscoped_on_agent_routes(
        self, client, test_tenant, pool,
    ):
        """role=admin + tenant_id=NULL is not a service account — 404, not a write."""
        from services.config import auth as auth_mod
        from services.config import users as users_service
        email = f"test-null-admin-{uuid.uuid4().hex[:8]}@example.com"
        user = await users_service.create_user(
            email=email, password="test-password-not-real", role="admin", tenant_id=None,
        )
        token = auth_mod.create_access_token(user)
        try:
            from httpx import ASGITransport, AsyncClient
            from services.config.app import app
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test",
                headers={"Authorization": f"Bearer {token}"},
            ) as stray:
                resp = await stray.get(f"/tenants/{test_tenant['slug']}/agents")
                assert resp.status_code == 404
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

    async def test_service_account_can_read_any_tenant_agents(
        self, test_tenant, pool,
    ):
        from httpx import ASGITransport, AsyncClient
        from services.config import auth as auth_mod
        from services.config.app import app
        email = f"test-svc-{uuid.uuid4().hex[:8]}@internal.yuviz.ai"
        row = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, tenant_id, is_service_account) "
            "VALUES ($1, $2, 'viewer', NULL, true) RETURNING *",
            email, auth_mod.hash_password("svc-not-real"),
        )
        user = dict(row)
        token = auth_mod.create_access_token(user)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test",
                headers={"Authorization": f"Bearer {token}"},
            ) as svc:
                resp = await svc.get(f"/tenants/{test_tenant['slug']}/agents")
                assert resp.status_code == 200
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])

    async def test_oversized_workflow_graph_is_422(self, client, test_tenant):
        nodes = [
            {"id": f"n{i}", "type": "agent", "position": {"x": 0, "y": i},
             "data": {"name": f"n{i}", "prompt": "x"}}
            for i in range(51)
        ]
        nodes[0]["type"] = "start"
        nodes[-1]["type"] = "end"
        nodes[-1]["data"]["disposition"] = "completed"
        resp = await client.put(
            f"/tenants/{test_tenant['slug']}/agents/{uuid.uuid4()}/workflow/draft",
            json={"graph": {"version": 1, "nodes": nodes, "edges": []}},
        )
        assert resp.status_code == 422

    async def test_create_agent_under_unknown_tenant_is_404(self, client):
        resp = await client.post(
            "/tenants/no-such-tenant/agents", json={"slug": "x", "name": "X"},
        )
        assert resp.status_code == 404

    async def test_update_agent_transfer_config(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support"},
        )
        agent_id = create.json()["id"]

        resp = await client.patch(
            f"/tenants/{test_tenant['slug']}/agents/{agent_id}",
            json={"transfer_type": "warm", "transfer_destination": "+18005550100"},
        )
        assert resp.status_code == 200
        assert resp.json()["transfer_type"] == "warm"

    async def test_create_agent_with_inline_provider_config_ids(self, client, test_tenant):
        stt = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        llm = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Ollama", "role": "llm", "engine": "ollama"},
        )
        tts = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Kokoro", "role": "tts", "engine": "kokoro"},
        )
        stt_id, llm_id, tts_id = stt.json()["id"], llm.json()["id"], tts.json()["id"]

        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={
                "slug": "support-agent", "name": "Support",
                "stt_config_id": stt_id, "llm_config_id": llm_id, "tts_config_id": tts_id,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["stt_config_id"] == stt_id
        assert body["llm_config_id"] == llm_id
        assert body["tts_config_id"] == tts_id

    async def test_create_agent_with_nonexistent_provider_config_id_is_400(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={
                "slug": "support-agent", "name": "Support",
                "stt_config_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert resp.status_code == 400

    async def test_create_agent_with_malformed_provider_config_id_is_400_not_500(self, client, test_tenant):
        """A non-UUID provider id is a 400, not an asyncpg DataError 500."""
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "stt_config_id": "not-a-uuid"},
        )
        assert resp.status_code == 400

    async def test_create_agent_rejects_wrong_role_provider_config(self, client, test_tenant):
        tts = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Kokoro", "role": "tts", "engine": "kokoro"},
        )
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "stt_config_id": tts.json()["id"]},
        )
        assert resp.status_code == 400

    async def test_create_agent_rejects_cross_tenant_provider_config(self, client, test_tenant, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Tenant", f"other-{uuid.uuid4().hex[:8]}",
        )
        try:
            other_stt = await client.post(
                f"/tenants/{other['id']}/providers",
                json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
            )
            resp = await client.post(
                f"/tenants/{test_tenant['slug']}/agents",
                json={"slug": "support-agent", "name": "Support", "stt_config_id": other_stt.json()["id"]},
            )
            assert resp.status_code == 400
        finally:
            await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_update_agent_rejects_wrong_role_provider_config(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support"},
        )
        agent_id = create.json()["id"]
        tts = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Kokoro", "role": "tts", "engine": "kokoro"},
        )
        resp = await client.patch(
            f"/tenants/{test_tenant['slug']}/agents/{agent_id}",
            json={"stt_config_id": tts.json()["id"]},
        )
        assert resp.status_code == 400

    async def test_create_agent_rejects_elevenlabs_provider_with_no_voice(self, client, test_tenant):
        tts = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs", "api_key_ref": "env:FAKE"},
        )
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "tts_config_id": tts.json()["id"]},
        )
        assert resp.status_code == 400
        assert "no voice selected" in resp.json()["detail"]

    async def test_update_agent_rejects_elevenlabs_provider_with_no_voice(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support"},
        )
        agent_id = create.json()["id"]
        tts = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs", "api_key_ref": "env:FAKE"},
        )
        resp = await client.patch(
            f"/tenants/{test_tenant['slug']}/agents/{agent_id}",
            json={"tts_config_id": tts.json()["id"]},
        )
        assert resp.status_code == 400
        assert "no voice selected" in resp.json()["detail"]

    async def test_elevenlabs_provider_with_voice_is_accepted(self, client, test_tenant):
        tts = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs", "api_key_ref": "env:FAKE", "voice": "abc123"},
        )
        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "tts_config_id": tts.json()["id"]},
        )
        assert resp.status_code == 201

    async def test_update_agent_invalid_transfer_type_is_422(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support"},
        )
        agent_id = create.json()["id"]

        resp = await client.patch(
            f"/tenants/{test_tenant['slug']}/agents/{agent_id}",
            json={"transfer_type": "not-a-real-type"},
        )
        assert resp.status_code == 422  # Pydantic Literal validation


class TestProviderConfigEndpoints:
    async def test_create_list_and_filter(self, client, test_tenant):
        await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram", "environment": "prod"},
        )
        await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Whisper", "role": "stt", "engine": "faster_whisper", "environment": "dev"},
        )

        resp = await client.get(f"/tenants/{test_tenant['id']}/providers", params={"role": "stt"})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        resp = await client.get(
            f"/tenants/{test_tenant['id']}/providers",
            params={"role": "stt", "environment": "prod"},
        )
        assert [p["engine"] for p in resp.json()] == ["deepgram"]

    async def test_nonexistent_tenant_id_is_404_not_500(self, client):
        """A well-formed but nonexistent tenant_id is a 404, not an FK-violation 500."""
        resp = await client.post(
            "/tenants/00000000-0000-0000-0000-000000000000/providers",
            json={"name": "X", "role": "stt", "engine": "deepgram"},
        )
        assert resp.status_code == 404

    async def test_malformed_tenant_id_is_400_not_500(self, client):
        resp = await client.post(
            "/tenants/not-a-uuid/providers",
            json={"name": "X", "role": "stt", "engine": "deepgram"},
        )
        assert resp.status_code == 400

    async def test_invalid_role_is_422(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Bad", "role": "not-a-role", "engine": "x"},
        )
        assert resp.status_code == 422

    async def test_get_update_delete_provider_config(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        provider_id = create.json()["id"]

        resp = await client.get(f"/providers/{provider_id}")
        assert resp.status_code == 200

        resp = await client.patch(f"/providers/{provider_id}", json={"model": "nova-3-medical"})
        assert resp.status_code == 200
        assert resp.json()["model"] == "nova-3-medical"

        resp = await client.delete(f"/providers/{provider_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/providers/{provider_id}")
        assert resp.status_code == 404

    async def test_delete_provider_in_use_is_409(self, client, test_tenant):
        stt = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        provider_id = stt.json()["id"]
        agent = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "stt_config_id": provider_id},
        )
        assert agent.status_code == 201

        resp = await client.delete(f"/providers/{provider_id}")
        assert resp.status_code == 409
        body = resp.json()
        assert body["resource_type"] == "agent"
        assert body["resource_count"] == 1
        assert body["resource_names"] == ["Support"]

        # Untouched — still there.
        still_there = await client.get(f"/providers/{provider_id}")
        assert still_there.status_code == 200

    async def test_delete_embedding_provider_in_use_by_kb_is_409(self, client, test_tenant, pool):
        # Embedding providers are referenced by knowledge_bases, not agents.
        emb = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Ollama Embedding", "role": "embedding", "engine": "ollama"},
        )
        provider_id = emb.json()["id"]
        kb_id = await pool.fetchval(
            "INSERT INTO knowledge_bases (tenant_id, slug, name, embedding_config_id) "
            "VALUES ($1, 'support-kb', 'Support KB', $2) RETURNING id",
            test_tenant["id"], provider_id,
        )
        try:
            resp = await client.delete(f"/providers/{provider_id}")
            assert resp.status_code == 409
            body = resp.json()
            assert body["resource_type"] == "knowledge_base"
            assert body["resource_count"] == 1
            assert body["resource_names"] == ["Support KB"]

            still_there = await client.get(f"/providers/{provider_id}")
            assert still_there.status_code == 200
        finally:
            await pool.execute("DELETE FROM knowledge_bases WHERE id = $1", kb_id)

    async def test_delete_provider_force_no_longer_bypasses_the_block(self, client, test_tenant):
        stt = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        provider_id = stt.json()["id"]
        agent = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "support-agent", "name": "Support", "stt_config_id": provider_id},
        )
        assert agent.status_code == 201

        resp = await client.delete(f"/providers/{provider_id}?force=true")
        assert resp.status_code == 409
        assert (await client.get(f"/providers/{provider_id}")).status_code == 200

    async def test_delete_provider_in_use_by_inactive_agent_is_409(self, client, test_tenant):
        stt = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        provider_id = stt.json()["id"]
        agent = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "idle-agent", "name": "Idle", "stt_config_id": provider_id},
        )
        assert agent.status_code == 201
        patched = await client.patch(
            f"/tenants/{test_tenant['slug']}/agents/{agent.json()['id']}", json={"status": "inactive"},
        )
        assert patched.status_code == 200

        resp = await client.delete(f"/providers/{provider_id}")
        assert resp.status_code == 409
        assert resp.json()["resource_names"] == ["Idle"]

    async def test_delete_provider_that_is_the_account_default_is_409(self, client, test_tenant):
        llm = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Groq", "role": "llm", "engine": "groq"},
        )
        provider_id = llm.json()["id"]
        set_default = await client.patch(f"/tenants/{test_tenant['id']}", json={"default_llm_config_id": provider_id})
        assert set_default.status_code == 200, set_default.text

        resp = await client.delete(f"/providers/{provider_id}")
        assert resp.status_code == 409
        assert resp.json()["resource_type"] == "tenant_default"
        assert resp.json()["resource_names"] == [test_tenant["name"]]
        assert (await client.get(f"/providers/{provider_id}")).status_code == 200

    async def test_a_deleted_provider_cannot_be_assigned_to_an_agent(self, client, test_tenant):
        stt = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        provider_id = stt.json()["id"]
        assert (await client.delete(f"/providers/{provider_id}")).status_code == 204

        resp = await client.post(
            f"/tenants/{test_tenant['slug']}/agents",
            json={"slug": "late-agent", "name": "Late", "stt_config_id": provider_id},
        )
        assert resp.status_code == 400

    async def test_account_default_must_be_the_accounts_own_live_provider_of_that_role(self, client, test_tenant, pool):
        stt = (await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )).json()["id"]
        wrong_role = await client.patch(f"/tenants/{test_tenant['id']}", json={"default_llm_config_id": stt})
        assert wrong_role.status_code == 400

        other = await pool.fetchrow(
            "INSERT INTO tenants (slug, name) VALUES ($1, 'Other') RETURNING id",
            f"other-{test_tenant['slug']}",
        )
        foreign = await pool.fetchval(
            "INSERT INTO provider_configs (tenant_id, name, role, engine) VALUES ($1, 'Theirs', 'stt', 'deepgram') RETURNING id",
            other["id"],
        )
        try:
            cross = await client.patch(f"/tenants/{test_tenant['id']}", json={"default_stt_config_id": str(foreign)})
            assert cross.status_code == 400

            assert (await client.delete(f"/providers/{stt}")).status_code == 204
            deleted = await client.patch(f"/tenants/{test_tenant['id']}", json={"default_stt_config_id": stt})
            assert deleted.status_code == 400
        finally:
            await pool.execute("DELETE FROM provider_configs WHERE id = $1", foreign)
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_an_elevenlabs_default_needs_a_voice_like_an_agent_assignment(self, client, test_tenant):
        tts = (await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "ElevenLabs", "role": "tts", "engine": "elevenlabs"},
        )).json()["id"]
        resp = await client.patch(f"/tenants/{test_tenant['id']}", json={"default_tts_config_id": tts})
        assert resp.status_code == 400
        assert "no voice selected" in resp.json()["detail"]

    async def test_setting_a_default_locks_the_provider_before_the_tenant(self, client, test_tenant, pool):
        """Provider delete locks provider then tenant; the default update must
        too, or the two deadlock."""
        import asyncio

        import asyncpg
        llm = (await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Groq", "role": "llm", "engine": "groq"},
        )).json()["id"]
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            await conn.execute("SELECT 1 FROM provider_configs WHERE id = $1 FOR UPDATE", llm)
            update = asyncio.create_task(
                client.patch(f"/tenants/{test_tenant['id']}", json={"default_llm_config_id": llm})
            )
            await asyncio.sleep(0.5)
            try:
                await conn.execute("SELECT 1 FROM tenants WHERE id = $1 FOR UPDATE NOWAIT", test_tenant["id"])
            except asyncpg.LockNotAvailableError:
                pytest.fail("update_tenant locked the tenant before the provider")
            finally:
                await tx.rollback()
        assert (await update).status_code == 200

    async def test_raw_sql_cannot_point_an_account_default_at_another_tenants_provider(self, test_tenant, pool):
        import asyncpg
        other = await pool.fetchval(
            "INSERT INTO tenants (slug, name) VALUES ($1, 'Other') RETURNING id", f"other2-{test_tenant['slug']}",
        )
        foreign = await pool.fetchval(
            "INSERT INTO provider_configs (tenant_id, name, role, engine) VALUES ($1, 'Theirs', 'tts', 'kokoro') RETURNING id",
            other,
        )
        try:
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await pool.execute("UPDATE tenants SET default_tts_config_id = $1 WHERE id = $2", foreign, test_tenant["id"])
        finally:
            await pool.execute("DELETE FROM provider_configs WHERE id = $1", foreign)
            await pool.execute("DELETE FROM tenants WHERE id = $1", other)

    async def test_voices_requires_elevenlabs_engine(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Kokoro", "role": "tts", "engine": "kokoro"},
        )
        resp = await client.get(f"/providers/{create.json()['id']}/voices")
        assert resp.status_code == 400

    async def test_update_to_blank_api_key_ref_is_400(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram", "api_key_ref": "env:DG_KEY"},
        )
        provider_id = create.json()["id"]

        resp = await client.patch(f"/providers/{provider_id}", json={"api_key_ref": ""})
        assert resp.status_code == 400

    async def test_update_to_blank_api_key_ref_with_new_api_key_is_allowed(self, client, test_tenant, monkeypatch):
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram", "api_key_ref": "env:DG_KEY"},
        )
        provider_id = create.json()["id"]

        resp = await client.patch(f"/providers/{provider_id}", json={"api_key_ref": "", "api_key": "dg_live_secret"})
        assert resp.status_code == 200
        assert resp.json()["api_key_ref"].startswith("enc:")

    async def test_update_with_both_api_key_and_a_real_api_key_ref_is_400(self, client, test_tenant, monkeypatch):
        # Both non-blank is ambiguous; a rotation pairs api_key with a blank api_key_ref.
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram", "api_key_ref": "env:DG_KEY"},
        )
        provider_id = create.json()["id"]

        resp = await client.patch(
            f"/providers/{provider_id}", json={"api_key_ref": "env:OTHER_KEY", "api_key": "dg_live_secret"},
        )
        assert resp.status_code == 400

    async def test_voices_requires_api_key_ref(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs"},
        )
        resp = await client.get(f"/providers/{create.json()['id']}/voices")
        assert resp.status_code == 400

    async def test_voices_unknown_provider_is_404(self, client):
        resp = await client.get("/providers/00000000-0000-0000-0000-000000000000/voices")
        assert resp.status_code == 404

    async def test_voices_success(self, client, test_tenant, monkeypatch):
        import httpx

        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs", "api_key_ref": "env:TEST_EL_KEY"},
        )
        provider_id = create.json()["id"]
        monkeypatch.setenv("TEST_EL_KEY", "fake-key-for-test")

        canned = {
            "voices": [
                {
                    "voice_id": "abc123", "name": "Rachel", "category": "premade",
                    "labels": {"gender": "female", "accent": "american"},
                    "preview_url": "https://example.com/preview.mp3",
                    "verified_languages": [
                        {
                            "language": "en", "model_id": "eleven_multilingual_v2",
                            "accent": "american", "locale": "en-US",
                            "preview_url": "https://example.com/preview-en.mp3",
                        },
                    ],
                },
            ],
        }

        real_get = httpx.AsyncClient.get

        async def fake_get(self, url, headers=None, **kwargs):
            if "elevenlabs.io" not in str(url):
                return await real_get(self, url, headers=headers, **kwargs)
            assert headers["xi-api-key"] == "fake-key-for-test"
            return httpx.Response(200, json=canned, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        resp = await client.get(f"/providers/{provider_id}/voices")
        assert resp.status_code == 200
        body = resp.json()
        assert body == [
            {
                "voice_id": "abc123", "name": "Rachel", "category": "premade",
                "labels": {"gender": "female", "accent": "american"},
                "preview_url": "https://example.com/preview.mp3",
                "verified_languages": [
                    {
                        "language": "en", "model_id": "eleven_multilingual_v2",
                        "accent": "american", "locale": "en-US",
                        "preview_url": "https://example.com/preview-en.mp3",
                    },
                ],
            },
        ]

    async def test_voices_verified_languages_defaults_to_empty_list(self, client, test_tenant, monkeypatch):
        """A missing verified_languages key comes through as []."""
        import httpx

        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs", "api_key_ref": "env:TEST_EL_KEY4"},
        )
        provider_id = create.json()["id"]
        monkeypatch.setenv("TEST_EL_KEY4", "fake-key-for-test")

        canned = {
            "voices": [
                {
                    "voice_id": "xyz789", "name": "Unverified", "category": "cloned",
                    "labels": {"language": "fr"}, "preview_url": None,
                },
            ],
        }
        real_get = httpx.AsyncClient.get

        async def fake_get(self, url, headers=None, **kwargs):
            if "elevenlabs.io" not in str(url):
                return await real_get(self, url, headers=headers, **kwargs)
            return httpx.Response(200, json=canned, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        resp = await client.get(f"/providers/{provider_id}/voices")
        assert resp.status_code == 200
        assert resp.json()[0]["verified_languages"] == []

    async def test_voices_network_error_is_clean_400_not_500(self, client, test_tenant, monkeypatch):
        """httpx.RequestError surfaces as a 400, not a 500."""
        import httpx

        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs", "api_key_ref": "env:TEST_EL_KEY2"},
        )
        provider_id = create.json()["id"]
        monkeypatch.setenv("TEST_EL_KEY2", "fake-key-for-test")

        real_get = httpx.AsyncClient.get

        async def fake_get(self, url, headers=None, **kwargs):
            if "elevenlabs.io" not in str(url):
                return await real_get(self, url, headers=headers, **kwargs)
            raise httpx.ConnectTimeout("connect timed out", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        resp = await client.get(f"/providers/{provider_id}/voices")
        assert resp.status_code == 400
        assert "ConnectTimeout" in resp.json()["detail"]

    async def test_voices_error_response_body_not_forwarded_to_caller(self, client, test_tenant, monkeypatch):
        """A non-200 ElevenLabs body is never echoed into the client error detail."""
        import httpx

        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "EL", "role": "tts", "engine": "elevenlabs", "api_key_ref": "env:TEST_EL_KEY3"},
        )
        provider_id = create.json()["id"]
        monkeypatch.setenv("TEST_EL_KEY3", "fake-key-for-test")

        secret_body = "super-secret-account-details-should-not-leak"
        real_get = httpx.AsyncClient.get

        async def fake_get(self, url, headers=None, **kwargs):
            if "elevenlabs.io" not in str(url):
                return await real_get(self, url, headers=headers, **kwargs)
            return httpx.Response(401, text=secret_body, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        resp = await client.get(f"/providers/{provider_id}/voices")
        assert resp.status_code == 400
        assert secret_body not in resp.json()["detail"]
        assert resp.json()["detail"] == "ElevenLabs Voices API returned 401"


class TestProviderConfigTenantScoping:
    """By-id /providers routes must reject another tenant's provider_config."""

    async def test_admin_cannot_get_another_tenants_provider(self, admin_client, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Tenant", f"other-{uuid.uuid4().hex[:8]}",
        )
        try:
            row = await pool.fetchrow(
                "INSERT INTO provider_configs (tenant_id, name, role, engine) "
                "VALUES ($1, $2, $3, $4) RETURNING *",
                other["id"], "Other's Deepgram", "stt", "deepgram",
            )
            resp = await admin_client.get(f"/providers/{row['id']}")
            assert resp.status_code == 403
        finally:
            await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_admin_cannot_update_another_tenants_provider(self, admin_client, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Tenant", f"other-{uuid.uuid4().hex[:8]}",
        )
        try:
            row = await pool.fetchrow(
                "INSERT INTO provider_configs (tenant_id, name, role, engine) "
                "VALUES ($1, $2, $3, $4) RETURNING *",
                other["id"], "Other's Deepgram", "stt", "deepgram",
            )
            resp = await admin_client.patch(f"/providers/{row['id']}", json={"model": "nova-3"})
            assert resp.status_code == 403
        finally:
            await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_admin_cannot_delete_another_tenants_provider(self, admin_client, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Tenant", f"other-{uuid.uuid4().hex[:8]}",
        )
        try:
            row = await pool.fetchrow(
                "INSERT INTO provider_configs (tenant_id, name, role, engine) "
                "VALUES ($1, $2, $3, $4) RETURNING *",
                other["id"], "Other's Deepgram", "stt", "deepgram",
            )
            resp = await admin_client.delete(f"/providers/{row['id']}")
            assert resp.status_code == 403
        finally:
            await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_admin_cannot_fetch_voices_for_another_tenants_provider(self, admin_client, pool):
        """Otherwise the request would spend the other tenant's ElevenLabs key."""
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Tenant", f"other-{uuid.uuid4().hex[:8]}",
        )
        try:
            row = await pool.fetchrow(
                "INSERT INTO provider_configs (tenant_id, name, role, engine, api_key_ref) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                other["id"], "Other's ElevenLabs", "tts", "elevenlabs", "env:OTHER_TENANT_KEY",
            )
            resp = await admin_client.get(f"/providers/{row['id']}/voices")
            assert resp.status_code == 403
        finally:
            await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_superadmin_can_access_any_tenants_provider(self, client, test_tenant):
        """A platform-scoped superadmin is exempt from the tenant check."""
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        resp = await client.get(f"/providers/{create.json()['id']}")
        assert resp.status_code == 200

    async def test_a_superadmin_with_a_leftover_tenant_id_is_narrowed_to_it(self, pool, test_tenant):
        """Platform scope is tenant_id IS NULL, not role: a superadmin with a tenant_id is confined to it."""
        own = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Own Tenant", f"own-{uuid.uuid4().hex[:8]}",
        )
        user = await users_service.create_user(
            email=f"scoped-superadmin-{uuid.uuid4().hex[:8]}@example.com",
            password="test-password-not-real", role="superadmin", tenant_id=own["id"],
        )
        token = auth.create_access_token(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                                   headers={"Authorization": f"Bearer {token}"}) as c:
                foreign = await c.post(
                    f"/tenants/{test_tenant['id']}/providers",
                    json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
                )
                mine = await c.post(
                    f"/tenants/{own['id']}/providers",
                    json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
                )
            assert foreign.status_code == 403
            assert mine.status_code == 201, mine.text
        finally:
            await pool.execute("UPDATE users SET deleted_at = now(), tenant_id = NULL WHERE id = $1", user["id"])
            await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", own["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", own["id"])

    async def test_viewer_with_no_tenant_id_is_unscoped(self, client, pool, test_tenant):
        """tenant_id=None exempts regardless of role (the Conversation service account is a viewer)."""
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        provider_id = create.json()["id"]

        user = await users_service.create_user(
            email=f"scoped-viewer-{uuid.uuid4().hex[:8]}@example.com",
            password="test-password-not-real", role="viewer", tenant_id=None,
        )
        token = auth.create_access_token(user)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as scoped_client:
                resp = await scoped_client.get(f"/providers/{provider_id}")
                assert resp.status_code == 200
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user["id"])


class TestToolProviderConfigEndpoints:
    """A missing credential is rejected at config time, not discovered at call time."""

    async def test_create_with_valid_api_key_ref(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={
                "name": "Book Appointment (Cal.com)", "tool_name": "book_appointment",
                "engine": "cal_com", "api_key_ref": "env:CAL_API_KEY", "extra": {"event_type_id": 123},
            },
        )
        assert resp.status_code == 201
        assert resp.json()["api_key_ref"] == "env:CAL_API_KEY"

    async def test_create_with_neither_api_key_ref_nor_api_key_is_400(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={"name": "X", "tool_name": "book_appointment", "engine": "cal_com"},
        )
        assert resp.status_code == 400

    async def test_create_with_api_key_encrypts_it(self, client, test_tenant, monkeypatch):
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={"name": "X", "tool_name": "book_appointment", "engine": "cal_com", "api_key": "cal_live_secret"},
        )
        assert resp.status_code == 201
        assert resp.json()["api_key_ref"].startswith("enc:")

    async def test_create_toolexec_engine_with_no_api_key_ref_succeeds(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={"name": "Custom APIs", "tool_name": "execute_api", "engine": "toolexec"},
        )
        assert resp.status_code == 201
        assert resp.json()["api_key_ref"] is None

    async def test_create_with_blank_api_key_ref_is_400(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={"name": "X", "tool_name": "book_appointment", "engine": "cal_com", "api_key_ref": "   "},
        )
        assert resp.status_code == 400

    async def test_update_to_blank_api_key_ref_is_400(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={
                "name": "X", "tool_name": "book_appointment", "engine": "cal_com",
                "api_key_ref": "env:CAL_API_KEY",
            },
        )
        tpc_id = create.json()["id"]

        resp = await client.patch(f"/tool-providers/{tpc_id}", json={"api_key_ref": ""})
        assert resp.status_code == 400

    async def test_update_to_blank_api_key_ref_with_new_api_key_is_allowed(self, client, test_tenant, monkeypatch):
        # A rotation: blank api_key_ref is the UI's placeholder alongside a new api_key.
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())
        create = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={
                "name": "X", "tool_name": "book_appointment", "engine": "cal_com",
                "api_key_ref": "env:CAL_API_KEY",
            },
        )
        tpc_id = create.json()["id"]

        resp = await client.patch(
            f"/tool-providers/{tpc_id}", json={"api_key_ref": "", "api_key": "cal_live_new_secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["api_key_ref"].startswith("enc:")

    async def test_update_with_api_key_encrypts_it(self, client, test_tenant, monkeypatch):
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())
        create = await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={
                "name": "X", "tool_name": "book_appointment", "engine": "cal_com",
                "api_key_ref": "env:CAL_API_KEY",
            },
        )
        tpc_id = create.json()["id"]

        resp = await client.patch(f"/tool-providers/{tpc_id}", json={"api_key": "cal_live_new_secret"})
        assert resp.status_code == 200
        assert resp.json()["api_key_ref"].startswith("enc:")


class TestAgentToolPolicyMaxChainDepth:
    """max_chain_depth is settable and clearable through the API."""

    async def test_create_and_patch_max_chain_depth(self, client, test_tenant, pool):
        agent = dict(await pool.fetchrow(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *",
            test_tenant["id"],
        ))
        tpc = (await client.post(
            f"/tenants/{test_tenant['id']}/tool-providers",
            json={"name": "Custom APIs", "tool_name": "execute_api", "engine": "toolexec"},
        )).json()

        create = await client.post(
            f"/agents/{agent['id']}/tool-policies",
            json={
                "tool_name": "execute_api", "tool_provider_config_id": tpc["id"],
                "max_chain_depth": 2,
            },
        )
        assert create.status_code == 201
        assert create.json()["max_chain_depth"] == 2

        patch = await client.patch(
            f"/agents/{agent['id']}/tool-policies/execute_api", json={"max_chain_depth": 3},
        )
        assert patch.status_code == 200
        assert patch.json()["max_chain_depth"] == 3

        # Explicit null clears it; exclude_unset must not treat it as "not sent".
        clear = await client.patch(
            f"/agents/{agent['id']}/tool-policies/execute_api", json={"max_chain_depth": None},
        )
        assert clear.status_code == 200
        assert clear.json()["max_chain_depth"] is None


class TestCarrierEndpoints:
    """Carrier CRUD routes."""

    async def test_create_list_and_get_carrier(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/carriers",
            json={
                "name": "Plivo Main", "provider": "plivo",
                "auth_id": "MAXXXXXXXXXXXXXXXXXX", "auth_token_ref": "env:PLIVO_AUTH_TOKEN",
                "carrier_account_ref": "MAXXXXXXXXXXXXXXXXXX",
            },
        )
        assert create.status_code == 201
        body = create.json()
        assert body["provider"] == "plivo"
        assert body["auth_token_ref"] == "env:PLIVO_AUTH_TOKEN"
        carrier_id = body["id"]

        resp = await client.get(f"/tenants/{test_tenant['id']}/carriers")
        assert resp.status_code == 200
        assert [c["id"] for c in resp.json()] == [carrier_id]

        resp = await client.get(f"/carriers/{carrier_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Plivo Main"

    async def test_invalid_provider_is_422(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/carriers",
            json={"name": "Bad", "provider": "not-a-real-carrier"},
        )
        assert resp.status_code == 422

    async def test_nonexistent_tenant_id_is_404_not_500(self, client):
        resp = await client.post(
            "/tenants/00000000-0000-0000-0000-000000000000/carriers",
            json={"name": "X", "provider": "plivo"},
        )
        assert resp.status_code == 404

    async def test_update_and_delete_carrier(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/carriers",
            json={"name": "Plivo Main", "provider": "plivo"},
        )
        carrier_id = create.json()["id"]

        resp = await client.patch(f"/carriers/{carrier_id}", json={"name": "Plivo Renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Plivo Renamed"

        resp = await client.delete(f"/carriers/{carrier_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/carriers/{carrier_id}")
        assert resp.status_code == 404


class TestPhoneNumberEndpoints:
    async def test_create_with_nonexistent_carrier_id_is_404_not_400(self, client, test_tenant):
        """An unknown carrier_id is a precise 404, like agent_id."""
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/phone-numbers",
            json={
                "did": f"test-did-{uuid.uuid4().hex[:8]}",
                "carrier_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert resp.status_code == 404
        assert "carrier" in resp.json()["detail"]

    async def test_create_with_malformed_carrier_id_is_400(self, client, test_tenant):
        resp = await client.post(
            f"/tenants/{test_tenant['id']}/phone-numbers",
            json={"did": f"test-did-{uuid.uuid4().hex[:8]}", "carrier_id": "not-a-uuid"},
        )
        assert resp.status_code == 400

    async def test_update_with_nonexistent_carrier_id_is_404(self, client, test_tenant):
        create = await client.post(
            f"/tenants/{test_tenant['id']}/phone-numbers",
            json={"did": f"test-did-{uuid.uuid4().hex[:8]}"},
        )
        phone_number_id = create.json()["id"]

        resp = await client.patch(
            f"/phone-numbers/{phone_number_id}",
            json={"carrier_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert resp.status_code == 404
        assert "carrier" in resp.json()["detail"]


class TestCallEndpoints:
    async def test_list_calls_for_unknown_tenant_is_404(self, client):
        resp = await client.get("/tenants/not-a-real-tenant-slug/calls")
        assert resp.status_code == 404

    async def test_list_and_get_call(self, client, test_tenant, pool):
        session_id = f"test-call-{uuid.uuid4().hex[:8]}"
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
            session_id, test_tenant["slug"],
        )

        resp = await client.get(f"/tenants/{test_tenant['slug']}/calls")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["session_id"] == session_id
        assert body["items"][0]["mode"] == "AI"

        resp = await client.get(f"/calls/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == session_id

        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)

    async def test_get_unknown_call_is_404(self, client):
        resp = await client.get("/calls/does-not-exist")
        assert resp.status_code == 404

    async def test_get_transcript_for_unknown_call_is_404(self, client):
        resp = await client.get("/calls/does-not-exist/transcript")
        assert resp.status_code == 404

    async def test_get_transcript(self, client, test_tenant, pool):
        session_id = f"test-call-{uuid.uuid4().hex[:8]}"
        await pool.execute(
            "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
            session_id, test_tenant["slug"],
        )
        await pool.execute(
            "INSERT INTO transcript_entries (session_id, turn_number, caller_text, ai_response) "
            "VALUES ($1, 1, 'hi', 'hello')",
            session_id,
        )

        resp = await client.get(f"/calls/{session_id}/transcript")
        assert resp.status_code == 200
        assert resp.json()[0]["caller_text"] == "hi"

        await pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", session_id)
        await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)

    async def test_tenant_admin_cannot_read_another_tenants_call(
        self, admin_client, test_tenant, pool,
    ):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Call Tenant", f"test-other-call-{uuid.uuid4().hex[:8]}",
        )
        session_id = f"test-call-{uuid.uuid4().hex[:8]}"
        try:
            await pool.execute(
                "INSERT INTO calls (session_id, tenant_id, direction, extracted_variables) "
                "VALUES ($1, $2, 'inbound', $3::jsonb)",
                session_id, other["slug"], '{"policy_number": "SECRET"}',
            )
            known_other = await admin_client.get(f"/calls/{session_id}")
            unknown = await admin_client.get("/calls/does-not-exist")
            assert known_other.status_code == unknown.status_code == 404
            assert known_other.json() == {"detail": f"call {session_id!r} not found"}

            own_id = f"test-call-{uuid.uuid4().hex[:8]}"
            await pool.execute(
                "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
                own_id, test_tenant["slug"],
            )
            own = await admin_client.get(f"/calls/{own_id}")
            assert own.status_code == 200
            assert own.json()["session_id"] == own_id
            await pool.execute("DELETE FROM calls WHERE session_id = $1", own_id)
        finally:
            await pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


class TestAuthEndpoints:
    async def test_login_succeeds_with_correct_credentials(self, anon_client, test_superadmin):
        resp = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "test-password-not-real"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert "access_token" in body
        assert body["user"]["email"] == test_superadmin["user"]["email"]
        assert "password_hash" not in body["user"]

    async def test_login_fails_with_wrong_password(self, anon_client, test_superadmin):
        resp = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "wrong-password"},
        )
        assert resp.status_code == 401

    async def test_login_fails_for_unknown_email(self, anon_client):
        resp = await anon_client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "anything"},
        )
        assert resp.status_code == 401

    async def test_me_requires_auth(self, anon_client):
        resp = await anon_client.get("/auth/me")
        assert resp.status_code == 401

    async def test_me_returns_current_user(self, client, test_superadmin):
        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == test_superadmin["user"]["email"]

    async def test_protected_endpoint_without_token_is_401(self, anon_client):
        resp = await anon_client.get("/tenants")
        assert resp.status_code == 401

    async def test_protected_endpoint_with_malformed_header_is_401(self, anon_client):
        resp = await anon_client.get("/tenants", headers={"Authorization": "not-a-bearer-token"})
        assert resp.status_code == 401

    async def test_viewer_can_read_but_not_write(self, viewer_client, test_tenant):
        get_resp = await viewer_client.get(f"/tenants/{test_tenant['slug']}")
        assert get_resp.status_code == 200

        patch_resp = await viewer_client.patch(f"/tenants/{test_tenant['id']}", json={"name": "Hijacked"})
        assert patch_resp.status_code == 403

    async def test_change_password_requires_auth(self, anon_client):
        resp = await anon_client.post(
            "/auth/change-password", json={"current_password": "x", "new_password": "newpassword123"},
        )
        assert resp.status_code == 401

    async def test_change_password_succeeds_and_old_password_stops_working(self, client, anon_client, test_superadmin):
        resp = await client.post(
            "/auth/change-password",
            json={"current_password": "test-password-not-real", "new_password": "brand-new-password"},
        )
        # Other sessions are signed out; this one gets a fresh token.
        assert resp.status_code == 200
        assert resp.json()["access_token"]

        old_login = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "test-password-not-real"},
        )
        assert old_login.status_code == 401

        new_login = await anon_client.post(
            "/auth/login",
            json={"email": test_superadmin["user"]["email"], "password": "brand-new-password"},
        )
        assert new_login.status_code == 200

    async def test_change_password_fails_with_wrong_current_password(self, client):
        resp = await client.post(
            "/auth/change-password",
            json={"current_password": "totally-wrong", "new_password": "brand-new-password"},
        )
        assert resp.status_code == 400

    async def test_change_password_rejects_too_short_new_password(self, client):
        resp = await client.post(
            "/auth/change-password",
            json={"current_password": "test-password-not-real", "new_password": "short"},
        )
        assert resp.status_code == 422


class TestUserEndpoints:
    # Users are created via invites now (see test_invites.py).
    async def test_post_users_route_is_gone(self, client):
        resp = await client.post(
            "/users", json={"email": "new@example.com", "password": "pw", "role": "viewer"},
        )
        assert resp.status_code in (404, 405)

    async def test_list_and_delete_user(self, client, pool):
        email = f"test-created-{uuid.uuid4().hex[:8]}@example.com"
        created = await users_service.create_user(email=email, password="a-real-password", role="admin")

        list_resp = await client.get("/users")
        assert any(u["email"] == email for u in list_resp.json())

        del_resp = await client.delete(f"/users/{created['id']}")
        assert del_resp.status_code == 204

    async def test_superadmin_tenant_filter_still_includes_superadmin_role_rows(
        self, client, test_tenant, pool,
    ):
        # The `role != 'superadmin'` exclusion is for non-superadmin actors only.
        email = f"tenant-scoped-superadmin-{uuid.uuid4().hex[:8]}@example.com"
        row = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, tenant_id) "
            "VALUES ($1, 'x', 'superadmin', $2) RETURNING *",
            email, test_tenant["id"],
        )
        try:
            resp = await client.get(f"/users?tenant_id={test_tenant['id']}")
            assert resp.status_code == 200
            assert email in {u["email"] for u in resp.json()}
        finally:
            await pool.execute("DELETE FROM users WHERE id = $1", row["id"])

    async def test_admin_cannot_update_another_user(self, admin_client, test_viewer):
        resp = await admin_client.patch(f"/users/{test_viewer['user']['id']}", json={"role": "admin"})
        assert resp.status_code == 403

    async def test_admin_cannot_delete_another_user(self, admin_client, test_viewer):
        resp = await admin_client.delete(f"/users/{test_viewer['user']['id']}")
        assert resp.status_code == 403

    async def test_viewer_cannot_update_or_delete_users(self, viewer_client, test_admin):
        resp = await viewer_client.patch(f"/users/{test_admin['user']['id']}", json={"role": "viewer"})
        assert resp.status_code == 403
        resp = await viewer_client.delete(f"/users/{test_admin['user']['id']}")
        assert resp.status_code == 403

    async def test_update_user_rejects_explicit_null_password_with_no_other_fields(self, client, test_viewer):
        resp = await client.patch(f"/users/{test_viewer['user']['id']}", json={"password": None})
        assert resp.status_code == 400

    async def test_service_account_hidden_from_list_and_protected(self, client, pool):
        row = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, is_service_account) "
            "VALUES ($1, 'x', 'viewer', true) RETURNING *",
            f"test-svc-{uuid.uuid4().hex[:8]}@internal.yuviz.ai",
        )
        svc_id = str(row["id"])
        try:
            resp = await client.get("/users")
            assert svc_id not in [u["id"] for u in resp.json()]

            resp = await client.patch(f"/users/{svc_id}", json={"role": "admin"})
            assert resp.status_code == 400

            resp = await client.delete(f"/users/{svc_id}")
            assert resp.status_code == 400
        finally:
            await pool.execute("DELETE FROM users WHERE id = $1", svc_id)

    async def test_viewer_service_account_with_null_tenant_cannot_read_other_tenants(self, pool, test_tenant):
        # Platform scope alone (tenant_id NULL) must not grant a viewer the superadmin cross-tenant read.
        svc_row = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, tenant_id, is_service_account) "
            "VALUES ($1, 'x', 'viewer', NULL, true) RETURNING *",
            f"test-svc-viewer-{uuid.uuid4().hex[:8]}@internal.yuviz.ai",
        )
        tenant_user_email = f"test-tenant-user-{uuid.uuid4().hex[:8]}@example.com"
        tenant_user = await users_service.create_user(
            email=tenant_user_email, password="a-real-password", role="viewer", tenant_id=test_tenant["id"],
        )
        token = auth.create_access_token(dict(svc_row))
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(
                transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"},
            ) as svc_client:
                resp = await svc_client.get("/users")
            assert resp.status_code == 200
            assert tenant_user_email not in {u["email"] for u in resp.json()}
        finally:
            await pool.execute("DELETE FROM users WHERE id = $1", svc_row["id"])
            await pool.execute("DELETE FROM users WHERE id = $1", tenant_user["id"])

    async def test_service_account_backfill_is_case_insensitive(self, pool):
        row = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, is_service_account) "
            "VALUES ($1, 'x', 'viewer', false) RETURNING id",
            f"Test-Mixed-Case-{uuid.uuid4().hex[:8]}@INTERNAL.yuviz.ai",
        )
        svc_id = row["id"]
        try:
            await pool.execute(
                "UPDATE users SET is_service_account = true "
                "WHERE lower(email) LIKE '%@internal.%' AND is_service_account = false",
            )
            flagged = await pool.fetchval("SELECT is_service_account FROM users WHERE id = $1", svc_id)
            assert flagged is True
        finally:
            await pool.execute("DELETE FROM users WHERE id = $1", svc_id)


class TestHealthEndpoint:
    async def test_health_check(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
