"""
Tests the actual HTTP layer (routing, request validation, status codes, error
mapping) in-process via httpx's ASGITransport — no live uvicorn process, but
every request really goes through FastAPI's routing/validation and really
hits Postgres + Redis (same conftest fixtures as the service-layer tests).
FastAPI's lifespan isn't triggered here since it only eagerly warms the same
lazy singletons db.py/cache.py already create on first use — nothing this
test needs depends on the lifespan hook specifically running.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import generate_key
from services.config import auth
from services.config import users as users_service
from services.config.app import app


@pytest.fixture
async def client(test_superadmin):
    # Pre-authenticated as superadmin by default — most of this file's tests
    # predate real auth and are about routing/validation/status codes, not
    # authorization itself; TestAuthEndpoints below covers login/401/403
    # explicitly with its own unauthenticated/role-restricted clients.
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

    # Cross-tenant disclosure fix: list_tenants/get_tenant used to be gated
    # on Depends(get_current_user) alone, no tenant scoping at all — a
    # tenant-scoped admin/viewer got every tenant's name/slug (the
    # customer list) and could read any tenant's full row by slug.
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
            # There's no documented ?tenant_id= on this endpoint at all —
            # this proves one can't be smuggled in to widen the result
            # regardless, the same "never trust client tenancy" contract
            # GET /users and GET /invites already enforce.
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
            # Same status and the identical detail *shape* either way — a
            # real tenant that isn't theirs must not be distinguishable
            # from a slug that doesn't exist (no existence oracle); the
            # message only ever echoes back the slug the caller already
            # supplied, so comparing the template rather than the exact
            # string (which necessarily differs by slug) is the right check.
            assert known_other.status_code == unknown.status_code == 404
            assert known_other.json() == {"detail": f"tenant {other['slug']!r} not found"}
            assert unknown.json() == {"detail": "tenant 'does-not-exist-at-all' not found"}
        finally:
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_get_own_tenant_by_slug_still_works_for_tenant_admin(self, admin_client, test_tenant):
        resp = await admin_client.get(f"/tenants/{test_tenant['slug']}")
        assert resp.status_code == 200
        assert resp.json()["slug"] == test_tenant["slug"]


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
            victim = await agents_service.create_agent(
                tenant_id=other["id"], slug="victim", name="Victim",
                system_prompt="secret prompt IP",
            )
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
        """A non-UUID-shaped string previously reached asyncpg's parameter
        binding directly, raising DataError — not a ValueError/LookupError,
        so app.py had no handler for it and it surfaced as a raw 500."""
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
        """Regression test: a well-formed but nonexistent tenant_id used to
        hit an unhandled ForeignKeyViolationError and return a bare 500 with
        Postgres internals in the traceback."""
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
        # Sending both non-blank is ambiguous, not a legitimate rotation
        # (that pairs api_key with a *blank* api_key_ref) — likely a UI bug
        # upstream, so this must not silently prefer one over the other.
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
        """Not every voice has been through ElevenLabs' language
        verification — a missing verified_languages key must come through
        as [], not be dropped or raise."""
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
        """httpx.RequestError (DNS failure, timeout, ...) must not reach the
        caller as a bare, unhandled 500."""
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
        """A non-200 ElevenLabs response (e.g. a 401 body with account
        details) must be logged server-side, never echoed into the client-
        facing error detail."""
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
    """A tenant-scoped admin/viewer must never read, edit, delete, or fetch
    voices for another tenant's provider_config by id — the bare
    /providers/{id} routes have no tenant_id in their URL path, so nothing
    scopes them except the explicit check in _authorize_provider()."""

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
        """The highest-stakes case: without this check, a cross-tenant
        request would resolve the OTHER tenant's real api_key_ref and spend
        its ElevenLabs quota, not just leak metadata."""
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
        """Superadmin is deliberately exempt from the scope check — same
        "unscoped" contract as everywhere else in this service."""
        create = await client.post(
            f"/tenants/{test_tenant['id']}/providers",
            json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
        )
        resp = await client.get(f"/providers/{create.json()['id']}")
        assert resp.status_code == 200

    async def test_superadmin_with_a_tenant_id_set_is_still_unscoped(self, pool, test_tenant):
        """Regression (found live): a real superadmin account can
        have a non-null tenant_id (a leftover default from account
        creation, unrelated to their actual access level) — the
        authorization check must key off role=="superadmin", not tenant_id
        being None, or a legitimate superadmin gets a spurious 403 browsing
        any tenant other than the one their own tenant_id happens to name."""
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Tenant", f"other-{uuid.uuid4().hex[:8]}",
        )
        user = await users_service.create_user(
            email=f"scoped-superadmin-{uuid.uuid4().hex[:8]}@example.com",
            password="test-password-not-real", role="superadmin", tenant_id=other["id"],
        )
        token = auth.create_access_token(user)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as scoped_client:
                create = await scoped_client.post(
                    f"/tenants/{test_tenant['id']}/providers",
                    json={"name": "Deepgram", "role": "stt", "engine": "deepgram"},
                )
                resp = await scoped_client.get(f"/providers/{create.json()['id']}")
                assert resp.status_code == 200
        finally:
            await pool.execute(
                "UPDATE users SET deleted_at = now(), tenant_id = NULL WHERE id = $1", user["id"],
            )
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_viewer_with_no_tenant_id_is_unscoped(self, client, pool, test_tenant):
        """Regression (found live): the Conversation Service's own
        internal service account (conversation-service@internal.yuviz.ai)
        is role=viewer with tenant_id=NULL — it legitimately reads provider
        configs across every tenant it serves calls for, one process
        handling all tenants. A role=="superadmin"-only exemption blocked
        this account entirely, and every live call silently fell back to
        agent_config.py's hardcoded legacy default (a generic greeting
        instead of the real configured one) because agent_resolver treats
        any RuntimeConfig fetch failure as "fall back," not as a hard
        error. tenant_id is None must independently exempt regardless of
        role, matching auth.py's own CurrentUser docstring contract."""
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
    """Regression coverage for the blank api_key_ref gap (2026-07-23): a
    tool_provider_config with no key silently passed creation and only
    failed at call time (provider_manager.py's _make_cal_com), which broke
    a live call. Now caught here instead."""

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
        # Both are optional in the schema now (an admin may supply either)
        # — the router itself requires at least one.
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
        # engine='toolexec' is internal infrastructure (services/toolexec/),
        # not a tenant credential — the Admin UI cannot create the row
        # agent_tool_policies.tool_provider_config_id requires if this 400s.
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
        # A cleared cal_com api_key_ref fails silently until the next live
        # booking attempt — must not go through without a replacement.
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
        # Not a clear — a rotation. The blank api_key_ref is the frontend's
        # placeholder for "nothing typed here", paired with a real
        # replacement in api_key.
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
    """FIX 1 (c): before this, there was no API path to set
    agent_tool_policies.max_chain_depth at all — the column existed but
    nothing could write it, so the per-agent override was dead on
    arrival regardless of what the executor did with it."""

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

        # Explicitly clearing it back to NULL (use the platform ceiling)
        # must also work — exclude_unset must not confuse "not sent" with
        # "sent as null".
        clear = await client.patch(
            f"/agents/{agent['id']}/tool-policies/execute_api", json={"max_chain_depth": None},
        )
        assert clear.status_code == 200
        assert clear.json()["max_chain_depth"] is None


class TestCarrierEndpoints:
    """DID Management platform (2026-07-23): carriers previously had no
    CRUD/router at all, only an existence-check helper used by
    phone_numbers' own validation."""

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
        """Regression test: carrier_id used to be unvalidated, so a bad value
        only surfaced via the app-wide FK-violation-to-400 handler — a
        precise 404 (matching agent_id/fallback_agent_id's own behavior)
        instead of a generic 400."""
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
        assert resp.status_code == 204

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
    # POST /users (the temp-password create path) was removed once the
    # invite-based path (routers/invites.py) was reachable end-to-end —
    # invite creation/lookup coverage lives in test_invites.py instead. The
    # cases below that only needed *a* live user now create one directly via
    # users_service.create_user(), the same helper conftest.py's own
    # test_admin/test_viewer fixtures use.
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
        # Review finding 3: is_superadmin was being conflated with "no
        # tenant_id filter given", so a superadmin's own ?tenant_id= filter
        # was silently applying the `role != 'superadmin'` exclusion meant
        # only for non-superadmin actors — a superadmin-role row in that
        # tenant would vanish from the actor who's allowed to see it.
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
        # PR #19 security finding 1: is_platform_scoped (lesson 24 —
        # tenant_id is None) answers *which tenant*, not *how privileged*.
        # GET /users has no authority gate beyond CONSOLE_ROLES, so a
        # viewer-role service account — role="viewer", tenant_id=NULL,
        # exactly the shape Conversation's/vobiz's real service accounts
        # authenticate as — is just as platform-scoped as a superadmin.
        # It must not inherit a superadmin's unscoped, cross-tenant,
        # role-unfiltered read just because its own tenant_id is also
        # NULL. Would fail (the tenant user below present in the response)
        # if the route gated the unscoped branch on scope alone.
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
