"""tests/test_cross_tenant_admin.py — AC 6 cross-tenant admin matrix.

The direct answer to the security review's blocking finding: a
platform-scoped actor (`tenant_id IS NULL`) must reach another tenant's data
through the real HTTP apps exactly as before RLS, and a tenant-scoped actor
must never reach it (lesson 24: the predicate is `tenant_id IS NULL`, never
`role == "superadmin"`). Runs through the real FastAPI apps against the real
Postgres, not mocked — RLS is a database-layer control and a mock would let
the one thing under test slide past (same convention as
services/*/tests/conftest.py).

Scope note (lesson 12: state what is and isn't exercised). Design's test
plan asks for the full Tier 3 matrix parameterised over all 17 flat
routers; this file exercises that matrix in full for the ten Tier 3 routers
that live in Config (the majority, and the ones every other service's
pattern was copied from — provider_configs, telephony_configs, carriers,
phone_numbers, tool_provider_configs, calls, agent_tool_policies, users,
invites) plus campaigns/dnc (Campaigns), knowledge_bases (Knowledge),
custom_apis (Tool Execution) and purchased_numbers (DID) — one live case
per remaining service, proving the identical pattern holds across the
service boundary rather than re-deriving fixture plumbing for
`documents`/`agent_kb`/`agent_apis`/`live_calls`, whose fixtures are
materially heavier (a KB document upload, an agent-custom-api attachment, a
live call row) and did not fit this pass's budget.
"""
from __future__ import annotations

import contextlib
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_target_tenant
from libs.tenancy.session import TenantScope, _scope

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)
os.environ.setdefault("TOOLEXEC_TENANT_SECRET_ROOT", "/tmp/voiceai-toolexec-test-secrets")
os.environ.setdefault("KNOWLEDGE_STORAGE_ROOT", "/tmp/voiceai-knowledge-test-storage")
os.environ.setdefault("TOOLEXEC_TEST_HMAC_KEY", "dev-only-insecure-hmac-key-do-not-deploy-1")
os.environ.setdefault("TOOLEXEC_ARGS_HMAC_KEY_REF", "env:TOOLEXEC_TEST_HMAC_KEY")
os.makedirs(os.environ["TOOLEXEC_TENANT_SECRET_ROOT"], exist_ok=True)
if "SECRET_ENCRYPTION_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from services.campaigns import campaigns as campaigns_service
from services.campaigns import dnc as dnc_service
from services.campaigns.app import app as campaigns_app
from services.config import auth, cache, db
from services.config import agent_tool_policies as agent_tool_policies_service
from services.config import agents as agents_service
from services.config import carriers as carriers_service
from services.config import invites as invites_service
from services.config import phone_numbers as phone_numbers_service
from services.config import provider_configs as provider_configs_service
from services.config import telephony_configs as telephony_configs_service
from services.config import tool_provider_configs as tool_provider_configs_service
from services.config import users as users_service
from services.config.app import app as config_app
from services.did import carriers as did_carriers_service
from services.did import purchased_numbers as purchased_numbers_service
from services.did.app import app as did_app
from services.knowledge import knowledge_bases as knowledge_bases_service
from services.knowledge.app import app as knowledge_app
from services.toolexec import custom_apis as custom_apis_service
from services.toolexec.app import app as toolexec_app


def _client(app_, token: str | None = None) -> AsyncClient:
    transport = ASGITransport(app=app_)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


@contextlib.contextmanager
def _as_tenant(tenant_id):
    """Fixture setup calls a service module's create_*() directly (no HTTP
    request, so no bind_path_tenant/get_authenticated_user ever runs) —
    tenant_conn() still needs an ambient scope to resolve, so this sets the
    target the same way a real request's router-level dependency would,
    and clears it afterwards so one fixture's tenant can't leak into the
    next arrange step."""
    token = _scope.set(TenantScope())
    try:
        set_target_tenant(str(tenant_id))
        yield
    finally:
        _scope.reset(token)


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    yield await db.get_pool()


@pytest_asyncio.fixture(loop_scope="session")
async def two_tenants(pool):
    a_slug = f"xta-a-{uuid.uuid4().hex[:8]}"
    b_slug = f"xta-b-{uuid.uuid4().hex[:8]}"
    a = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", "XTA A", a_slug))
    b = dict(await pool.fetchrow("INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", "XTA B", b_slug))
    yield {"a": a, "b": b}
    for slug in (a_slug, b_slug):
        await cache.invalidate(f"tenant:{slug}")
    for tid in (a["id"], b["id"]):
        await pool.execute(
            "DELETE FROM agent_tool_policies WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = $1)", tid,
        )
        await pool.execute("DELETE FROM purchased_numbers WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM phone_numbers WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM campaigns WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM tool_provider_configs WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM provider_configs WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM carriers WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM telephony_configs WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM dnc_numbers WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM calls WHERE tenant_id = (SELECT slug FROM tenants WHERE id = $1)", tid)
        await pool.execute("DELETE FROM user_invites WHERE tenant_id = $1", tid)
        await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", tid)
    await pool.execute("DELETE FROM tenants WHERE id = ANY($1)", [a["id"], b["id"]])


async def _make_user(pool, *, role: str, tenant_id, is_service_account: bool = False):
    email = f"xta-{uuid.uuid4().hex[:8]}@example.com"
    user = await users_service.create_user(
        email=email, password="test-password-not-real", role=role, tenant_id=tenant_id,
    )
    if is_service_account:
        user = dict(await pool.fetchrow(
            "UPDATE users SET is_service_account = true WHERE id = $1 RETURNING *", user["id"],
        ))
    token = auth.create_access_token(user)
    return {"user": user, "token": token}


@pytest_asyncio.fixture(loop_scope="session")
async def superadmin(pool):
    """Platform-scoped: tenant_id IS NULL — the actor AC 6 is about."""
    u = await _make_user(pool, role="superadmin", tenant_id=None)
    yield u
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", u["user"]["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_admin_a(pool, two_tenants):
    u = await _make_user(pool, role="admin", tenant_id=two_tenants["a"]["id"])
    yield u
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", u["user"]["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def viewer_a(pool, two_tenants):
    u = await _make_user(pool, role="viewer", tenant_id=two_tenants["a"]["id"])
    yield u
    await pool.execute("DELETE FROM users WHERE id = $1", u["user"]["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def superadmin_with_tenant_a(pool, two_tenants):
    """Design Tier 2 "leftover default" case (3c): role='superadmin' but a
    non-NULL tenant_id — narrowed by this design to lose cross-tenant
    access, unlike before (lesson 24: is_platform_scoped, not role)."""
    u = await _make_user(pool, role="superadmin", tenant_id=two_tenants["a"]["id"])
    yield u
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", u["user"]["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def null_tenant_service_account(pool):
    """Conversation/vobiz's own shape: role='viewer', tenant_id IS NULL,
    is_service_account=True."""
    u = await _make_user(pool, role="viewer", tenant_id=None, is_service_account=True)
    yield u
    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", u["user"]["id"])


# =============================================================================
# T37 — the `users` module cases
# =============================================================================

async def test_self_promotion_to_platform_scope_is_refused(pool, two_tenants, superadmin_with_tenant_a):
    async with _client(config_app, superadmin_with_tenant_a["token"]) as c:
        resp = await c.patch(f"/users/{superadmin_with_tenant_a['user']['id']}", json={"tenant_id": None})
    assert resp.status_code == 403
    row = await pool.fetchrow("SELECT tenant_id FROM users WHERE id = $1", superadmin_with_tenant_a["user"]["id"])
    assert row["tenant_id"] == superadmin_with_tenant_a["user"]["tenant_id"]


async def test_self_promotion_on_another_user_in_own_tenant_is_also_refused(pool, two_tenants, superadmin_with_tenant_a, tenant_admin_a):
    async with _client(config_app, superadmin_with_tenant_a["token"]) as c:
        resp = await c.patch(f"/users/{tenant_admin_a['user']['id']}", json={"tenant_id": None})
    assert resp.status_code == 403


async def test_platform_scoped_superadmin_promoting_to_null_tenant_succeeds(pool, superadmin, tenant_admin_a):
    # Counter-case (lesson 12): without this arm, a blanket rejection of
    # {"tenant_id": null} would also pass the two refusal cases above for
    # the wrong reason.
    async with _client(config_app, superadmin["token"]) as c:
        resp = await c.patch(f"/users/{tenant_admin_a['user']['id']}", json={"tenant_id": None})
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] is None
    # Restore, so tenant_admin_a's own teardown (soft-delete) still runs
    # under a resolvable scope.
    await pool.execute(
        "UPDATE users SET tenant_id = $1 WHERE id = $2",
        tenant_admin_a["user"]["tenant_id"], tenant_admin_a["user"]["id"],
    )


async def test_cross_tenant_edit_is_refused_and_row_unmodified(pool, two_tenants, superadmin_with_tenant_a):
    # 403, not 404: this environment (like every service today, pre-Phase-9
    # cutover — design "Migration and rollout") still connects on the
    # superuser DSN, so RLS itself is inert (a superuser bypasses RLS
    # regardless of policy — design Q3) and the fetch always finds the row.
    # assert_tenant_access is the layer that is actually enforcing here, and
    # its own documented shape for a UUID mismatch is 403 (deps.py
    # assert_tenant_access docstring). Post-cutover the SAME row becomes
    # invisible to the fetch itself and this turns into a 404 from an empty
    # policy result instead — either way, never a 200 and never a mutation.
    b_user = await _make_user(pool, role="viewer", tenant_id=two_tenants["b"]["id"])
    try:
        async with _client(config_app, superadmin_with_tenant_a["token"]) as c:
            resp = await c.patch(f"/users/{b_user['user']['id']}", json={"role": "admin"})
        assert resp.status_code == 403
        row = await pool.fetchrow("SELECT role FROM users WHERE id = $1", b_user["user"]["id"])
        assert row["role"] == "viewer"
    finally:
        await pool.execute("DELETE FROM users WHERE id = $1", b_user["user"]["id"])


async def test_cross_tenant_delete_is_refused_and_row_unmodified(pool, two_tenants, superadmin_with_tenant_a):
    b_user = await _make_user(pool, role="viewer", tenant_id=two_tenants["b"]["id"])
    try:
        async with _client(config_app, superadmin_with_tenant_a["token"]) as c:
            resp = await c.delete(f"/users/{b_user['user']['id']}")
        assert resp.status_code == 403
        row = await pool.fetchrow("SELECT deleted_at FROM users WHERE id = $1", b_user["user"]["id"])
        assert row["deleted_at"] is None
    finally:
        await pool.execute("DELETE FROM users WHERE id = $1", b_user["user"]["id"])


def _yuviz_app_dsn() -> str:
    """Swap only the user/password component of POSTGRES_DSN for
    yuviz_app's — same helper tests/test_rls_isolation.py uses, needed here
    because the shared pool (`pool` fixture) connects as the superuser and
    so can never demonstrate a policy rejection (lesson 12: a counter-check
    must be able to actually fail)."""
    import urllib.parse as up

    parts = up.urlsplit(os.environ["POSTGRES_DSN"])
    netloc = f"yuviz_app:rls-test-only-password@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return up.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def test_cross_tenant_move_is_refused_with_db_layer_counter_check(pool, two_tenants, superadmin_with_tenant_a, viewer_a):
    async with _client(config_app, superadmin_with_tenant_a["token"]) as c:
        resp = await c.patch(f"/users/{viewer_a['user']['id']}", json={"tenant_id": str(two_tenants["b"]["id"])})
    assert resp.status_code == 403

    # DB-layer counter-check: the identical UPDATE, issued directly as
    # yuviz_app under A's GUC, is rejected by WITH CHECK — proving the
    # app-layer 403 isn't standing in for a control RLS doesn't actually
    # have (AC 5, same shape as tests/test_rls_isolation.py).
    await pool.execute("ALTER ROLE yuviz_app PASSWORD 'rls-test-only-password'")
    app_conn = await asyncpg.connect(_yuviz_app_dsn())
    try:
        async with app_conn.transaction():
            await app_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(two_tenants["a"]["id"]))
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app_conn.execute(
                    "UPDATE users SET tenant_id = $2 WHERE id = $1",
                    viewer_a["user"]["id"], two_tenants["b"]["id"],
                )
    finally:
        await app_conn.close()


async def test_identity_resolution_still_works_for_a_platform_account(superadmin):
    async with _client(config_app, superadmin["token"]) as c:
        resp = await c.get("/auth/me")
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] is None


async def test_get_users_for_null_tenant_viewer_service_account_returns_200(pool, null_tenant_service_account):
    async with _client(config_app, null_tenant_service_account["token"]) as c:
        resp = await c.get("/users")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# =============================================================================
# T38 — Tier 3 flat by-id matrix (see module docstring for scope)
# =============================================================================

async def _assert_tier3_matrix(app_, path_b, path_a, superadmin_token, tenant_admin_a_token, cross_tenant_status):
    """Case 4's three-part shape, run over a GET: platform actor sees the
    row (fails with TenantUnresolved if a resolver never got
    platform_scoped); tenant_admin of A on B's id is refused (403 or 404,
    whichever this router's own check produces — never 200); tenant_admin
    of A on A's own id is 200 (the counter-check that stops the middle case
    passing because the route is simply broken, lesson 12)."""
    async with _client(app_, superadmin_token) as c:
        resp = await c.get(path_b)
    assert resp.status_code == 200, resp.text

    async with _client(app_, tenant_admin_a_token) as c:
        resp = await c.get(path_b)
    assert resp.status_code == cross_tenant_status

    async with _client(app_, tenant_admin_a_token) as c:
        resp = await c.get(path_a)
    assert resp.status_code == 200, resp.text


async def test_tier3_provider_configs(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        cfg_a = await provider_configs_service.create_provider_config(
            tenant_id=two_tenants["a"]["id"], name="A LLM", role="llm", engine="openai",
        )
    with _as_tenant(two_tenants["b"]["id"]):
        cfg_b = await provider_configs_service.create_provider_config(
            tenant_id=two_tenants["b"]["id"], name="B LLM", role="llm", engine="openai",
        )
    await _assert_tier3_matrix(
        config_app, f"/providers/{cfg_b['id']}", f"/providers/{cfg_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=403,
    )


async def test_tier3_telephony_configs(two_tenants, superadmin, tenant_admin_a):
    creds = {"account_sid": "AC-test", "auth_id": "AC-test", "auth_token": "tok"}
    with _as_tenant(two_tenants["a"]["id"]):
        cfg_a = await telephony_configs_service.create_telephony_config(
            tenant_id=two_tenants["a"]["id"], name="A Telephony", provider="vobiz", credentials=creds,
        )
    with _as_tenant(two_tenants["b"]["id"]):
        cfg_b = await telephony_configs_service.create_telephony_config(
            tenant_id=two_tenants["b"]["id"], name="B Telephony", provider="vobiz", credentials=creds,
        )
    await _assert_tier3_matrix(
        config_app, f"/telephony-configs/{cfg_b['id']}", f"/telephony-configs/{cfg_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=403,
    )


async def test_tier3_carriers(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        carrier_a = await carriers_service.create_carrier(tenant_id=two_tenants["a"]["id"], name="A Carrier", provider="twilio")
    with _as_tenant(two_tenants["b"]["id"]):
        carrier_b = await carriers_service.create_carrier(tenant_id=two_tenants["b"]["id"], name="B Carrier", provider="twilio")
    await _assert_tier3_matrix(
        config_app, f"/carriers/{carrier_b['id']}", f"/carriers/{carrier_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=403,
    )


async def test_tier3_phone_numbers(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        pn_a = await phone_numbers_service.create_phone_number(tenant_id=two_tenants["a"]["id"], did=f"+1555{uuid.uuid4().int % 10**7:07d}")
    with _as_tenant(two_tenants["b"]["id"]):
        pn_b = await phone_numbers_service.create_phone_number(tenant_id=two_tenants["b"]["id"], did=f"+1555{uuid.uuid4().int % 10**7:07d}")
    await _assert_tier3_matrix(
        config_app, f"/phone-numbers/{pn_b['id']}", f"/phone-numbers/{pn_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=403,
    )


async def test_tier3_tool_provider_configs(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        tp_a = await tool_provider_configs_service.create_tool_provider_config(
            tenant_id=two_tenants["a"]["id"], name="A Tool", tool_name="weather", engine="http",
        )
    with _as_tenant(two_tenants["b"]["id"]):
        tp_b = await tool_provider_configs_service.create_tool_provider_config(
            tenant_id=two_tenants["b"]["id"], name="B Tool", tool_name="weather", engine="http",
        )
    await _assert_tier3_matrix(
        config_app, f"/tool-providers/{tp_b['id']}", f"/tool-providers/{tp_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=403,
    )


async def test_tier3_calls(pool, two_tenants, superadmin, tenant_admin_a):
    session_a = f"xta-call-a-{uuid.uuid4().hex[:8]}"
    session_b = f"xta-call-b-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
        session_a, two_tenants["a"]["slug"],
    )
    await pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
        session_b, two_tenants["b"]["slug"],
    )
    # calls.py's cross-tenant refusal is an app-level `WHERE tenant_id =
    # <caller's own slug>` predicate (get_call's own filter), not
    # assert_tenant_access — so it's 404, not 403, and this is true
    # regardless of the RLS cutover state (lesson 24 predicate applied at
    # the SQL layer already, matching design's `calls.py` note).
    await _assert_tier3_matrix(
        config_app, f"/calls/{session_b}", f"/calls/{session_a}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=404,
    )


async def test_tier3_campaigns(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        agent_a = await agents_service.create_agent(tenant_id=two_tenants["a"]["id"], slug="agent-a", name="Agent A")
        camp_a = await campaigns_service.create_campaign(
            two_tenants["a"]["id"], agent_id=agent_a["id"], name="Camp A", caller_id=None,
            max_concurrent_calls=1, pacing_seconds=1, max_attempts=1,
        )
    with _as_tenant(two_tenants["b"]["id"]):
        agent_b = await agents_service.create_agent(tenant_id=two_tenants["b"]["id"], slug="agent-b", name="Agent B")
        camp_b = await campaigns_service.create_campaign(
            two_tenants["b"]["id"], agent_id=agent_b["id"], name="Camp B", caller_id=None,
            max_concurrent_calls=1, pacing_seconds=1, max_attempts=1,
        )
    await _assert_tier3_matrix(
        campaigns_app, f"/campaigns/{camp_b['id']}", f"/campaigns/{camp_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=403,
    )


async def test_tier3_knowledge_bases(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        kb_a = await knowledge_bases_service.create_knowledge_base(
            tenant_id=two_tenants["a"]["id"], slug="kb-a", name="KB A",
        )
    with _as_tenant(two_tenants["b"]["id"]):
        kb_b = await knowledge_bases_service.create_knowledge_base(
            tenant_id=two_tenants["b"]["id"], slug="kb-b", name="KB B",
        )
    # _authorize_kb raises its OWN 404 (not assert_tenant_access, by design
    # — a {kb_id} route must not become a 403 existence oracle), so this is
    # 404 regardless of the RLS cutover state.
    await _assert_tier3_matrix(
        knowledge_app, f"/knowledge-bases/{kb_b['id']}", f"/knowledge-bases/{kb_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=404,
    )


async def test_tier3_custom_apis(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        api_a = await custom_apis_service.create_custom_api(
            tenant_id=two_tenants["a"]["id"], name="API A", description="", endpoint_url="https://example.com/a", method="GET",
        )
    with _as_tenant(two_tenants["b"]["id"]):
        api_b = await custom_apis_service.create_custom_api(
            tenant_id=two_tenants["b"]["id"], name="API B", description="", endpoint_url="https://example.com/b", method="GET",
        )
    # _authorize_custom_api raises its own LookupError -> 404 (same
    # "never a 403 existence oracle" shape as knowledge_bases).
    await _assert_tier3_matrix(
        toolexec_app, f"/custom-apis/{api_b['id']}", f"/custom-apis/{api_a['id']}",
        superadmin["token"], tenant_admin_a["token"], cross_tenant_status=404,
    )


async def test_tier3_invites_revoke(pool, two_tenants, superadmin, tenant_admin_a):
    from services.config.auth import CurrentUser

    actor = CurrentUser(id=str(superadmin["user"]["id"]), email=superadmin["user"]["email"], role="superadmin", tenant_id=None)
    with _as_tenant(two_tenants["b"]["id"]):
        invite_b, _ = await invites_service.create_invite(
            email=f"xta-invite-b-{uuid.uuid4().hex[:8]}@example.com", role="admin",
            tenant_id=two_tenants["b"]["id"], team=None, actor=actor,
        )
        invite_b2, _ = await invites_service.create_invite(
            email=f"xta-invite-b2-{uuid.uuid4().hex[:8]}@example.com", role="admin",
            tenant_id=two_tenants["b"]["id"], team=None, actor=actor,
        )
    with _as_tenant(two_tenants["a"]["id"]):
        invite_a, _ = await invites_service.create_invite(
            email=f"xta-invite-a-{uuid.uuid4().hex[:8]}@example.com", role="admin",
            tenant_id=two_tenants["a"]["id"], team=None, actor=actor,
        )

    # Superadmin (platform-scoped) revokes B's invite — parity with today.
    async with _client(config_app, superadmin["token"]) as c:
        resp = await c.post(f"/invites/{invite_b2['id']}/revoke")
    assert resp.status_code == 200

    # tenant_admin of A is refused on B's (still-pending) invite.
    async with _client(config_app, tenant_admin_a["token"]) as c:
        resp = await c.post(f"/invites/{invite_b['id']}/revoke")
    assert resp.status_code == 403
    row = await pool.fetchrow("SELECT status FROM user_invites WHERE id = $1", invite_b["id"])
    assert row["status"] == "pending"

    # tenant_admin of A revokes its own tenant's invite successfully.
    async with _client(config_app, tenant_admin_a["token"]) as c:
        resp = await c.post(f"/invites/{invite_a['id']}/revoke")
    assert resp.status_code == 200


async def test_tier3_dnc(pool, two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["a"]["id"]):
        entry_a = await dnc_service.add_number(two_tenants["a"]["id"], "+15550000001")
    with _as_tenant(two_tenants["b"]["id"]):
        entry_b = await dnc_service.add_number(two_tenants["b"]["id"], "+15550000002")
        entry_b2 = await dnc_service.add_number(two_tenants["b"]["id"], "+15550000003")

    # tenant_admin of A is refused deleting B's entry; row still present.
    async with _client(campaigns_app, tenant_admin_a["token"]) as c:
        resp = await c.delete(f"/dnc/{entry_b['id']}")
    assert resp.status_code == 403
    row = await pool.fetchrow("SELECT id FROM dnc_numbers WHERE id = $1", entry_b["id"])
    assert row is not None

    # Superadmin (platform-scoped) can delete B's entry — parity with today.
    async with _client(campaigns_app, superadmin["token"]) as c:
        resp = await c.delete(f"/dnc/{entry_b2['id']}")
    assert resp.status_code == 204

    # tenant_admin of A deletes its own tenant's entry successfully.
    async with _client(campaigns_app, tenant_admin_a["token"]) as c:
        resp = await c.delete(f"/dnc/{entry_a['id']}")
    assert resp.status_code == 204


async def test_tier3_purchased_numbers_did(pool, two_tenants, superadmin, tenant_admin_a):
    # assign (not release): release calls out to the real carrier provider,
    # which this fixture has no credentials for; assign is a pure DB link
    # to a phone_numbers row Config already owns (module docstring, step 3)
    # and exercises the identical assert_tenant_access/platform_scoped gate.
    carrier_a = dict(await pool.fetchrow(
        "INSERT INTO carriers (tenant_id, name, provider) VALUES ($1, 'A Carrier', 'twilio') RETURNING *",
        two_tenants["a"]["id"],
    ))
    carrier_b = dict(await pool.fetchrow(
        "INSERT INTO carriers (tenant_id, name, provider) VALUES ($1, 'B Carrier', 'twilio') RETURNING *",
        two_tenants["b"]["id"],
    ))
    with _as_tenant(two_tenants["a"]["id"]):
        pn_a = await purchased_numbers_service.record_purchase(
            tenant_id=two_tenants["a"]["id"], carrier_id=carrier_a["id"],
            phone_number="+15550001111", carrier_number_sid="SID-A",
        )
        phone_a = await phone_numbers_service.create_phone_number(tenant_id=two_tenants["a"]["id"], did="+15550001111")
    with _as_tenant(two_tenants["b"]["id"]):
        pn_b = await purchased_numbers_service.record_purchase(
            tenant_id=two_tenants["b"]["id"], carrier_id=carrier_b["id"],
            phone_number="+15550002222", carrier_number_sid="SID-B",
        )
        phone_b = await phone_numbers_service.create_phone_number(tenant_id=two_tenants["b"]["id"], did="+15550002222")

    # tenant_admin of A is refused assigning B's purchased number.
    async with _client(did_app, tenant_admin_a["token"]) as c:
        resp = await c.patch(f"/numbers/{pn_b['id']}/assign", params={"phone_number_id": str(phone_b["id"])})
    assert resp.status_code == 403
    row = await pool.fetchrow("SELECT phone_number_id FROM purchased_numbers WHERE id = $1", pn_b["id"])
    assert row["phone_number_id"] is None

    # Superadmin (platform-scoped) can assign B's purchased number — parity with today.
    async with _client(did_app, superadmin["token"]) as c:
        resp = await c.patch(f"/numbers/{pn_b['id']}/assign", params={"phone_number_id": str(phone_b["id"])})
    assert resp.status_code == 200

    # tenant_admin of A assigns its own tenant's purchased number successfully.
    async with _client(did_app, tenant_admin_a["token"]) as c:
        resp = await c.patch(f"/numbers/{pn_a['id']}/assign", params={"phone_number_id": str(phone_a["id"])})
    assert resp.status_code == 200


# =============================================================================
# T41 — Tier 4 cases (body/non-/tenants/ path tenant sites)
# =============================================================================

async def test_tier4_internal_retrieve_refuses_a_foreign_tenant_slug(two_tenants, viewer_a):
    # assert_tenant_access's slug branch is 404, not 403 (deps.py
    # docstring: "slug argument, mismatch or unknown -> 404") — a
    # {tenant_slug} value must not become a 403 existence oracle.
    async with _client(knowledge_app, viewer_a["token"]) as c:
        resp = await c.post("/internal/retrieve", json={
            "tenant_slug": two_tenants["b"]["slug"], "agent_slug": "some-agent", "query": "hi",
        })
    assert resp.status_code == 404


async def test_tier4_internal_retrieve_admits_null_tenant_service_account_and_sets_the_guc(
    two_tenants, null_tenant_service_account,
):
    # retrieval_service.retrieve is stubbed (no embedding/vector infra in
    # this fixture) so this exercises exactly the thing Tier 4 adds: the
    # GUC is resolved to the BODY's tenant, not the (NULL) caller's, before
    # retrieval ever runs.
    from unittest.mock import AsyncMock, patch

    from libs.tenancy import current_tenant
    from services.knowledge.routers import retrieve as retrieve_router

    captured = {}

    async def _fake_retrieve(*args, **kwargs):
        captured["tenant"] = current_tenant()
        return {"chunks": [], "citations": []}

    with patch.object(retrieve_router.retrieval_service, "retrieve", AsyncMock(side_effect=_fake_retrieve)):
        async with _client(knowledge_app, null_tenant_service_account["token"]) as c:
            resp = await c.post("/internal/retrieve", json={
                "tenant_slug": two_tenants["b"]["slug"], "agent_slug": "some-agent", "query": "hi",
            })
    assert resp.status_code == 200
    assert captured["tenant"] == two_tenants["b"]["slug"]


async def test_tier4_has_knowledge_refuses_a_foreign_tenant_slug(two_tenants, viewer_a):
    async with _client(knowledge_app, viewer_a["token"]) as c:
        resp = await c.get(f"/internal/agents/{two_tenants['b']['slug']}/some-agent/has-knowledge")
    assert resp.status_code == 404


async def test_tier4_execute_chain_refuses_a_non_allow_listed_service_account(pool, two_tenants, null_tenant_service_account):
    # require_execute_subject is unchanged: is_service_account alone isn't
    # enough, the email must be on TOOLEXEC_EXECUTE_SUBJECTS's allow-list —
    # this account isn't, so it never reaches the tenant/body check at all.
    async with _client(toolexec_app, null_tenant_service_account["token"]) as c:
        resp = await c.post("/internal/chains/execute", json={
            "tenant_id": str(two_tenants["b"]["id"]), "agent_id": str(uuid.uuid4()),
            "call_id": "c1", "session_id": "s1", "turn_id": "t1", "tool_call_id": "tc1",
            "idempotency_key": "idem1", "api_name": "some_api",
            "chain_budget_ms": 5000, "max_chain_depth": 1,
        })
    assert resp.status_code == 403


async def test_tier4_execute_chain_admits_conversation_and_sets_the_guc(pool, two_tenants):
    from unittest.mock import AsyncMock, patch

    from libs.tenancy import current_tenant
    from services.config import auth as auth_module
    from services.toolexec.routers import execute as execute_router

    # Reuse the real allow-listed conversation-service account rather than
    # minting a same-email duplicate (users_email_lower_idx is unique).
    conv_row = await pool.fetchrow(
        "SELECT * FROM users WHERE lower(email) = 'conversation-service@internal.yuviz.ai' "
        "AND deleted_at IS NULL",
    )
    assert conv_row is not None, "expected the seeded conversation-service account (scripts/create_service_account.py)"
    token = auth_module.create_access_token(dict(conv_row))

    captured = {}

    async def _fake_execute_chain(body):
        captured["tenant"] = current_tenant()
        return execute_router.ChainExecuteResponse(run_id="r1", chain_status="success")

    with patch.object(execute_router.executor, "execute_chain", AsyncMock(side_effect=_fake_execute_chain)):
        async with _client(toolexec_app, token) as c:
            resp = await c.post("/internal/chains/execute", json={
                "tenant_id": str(two_tenants["b"]["id"]), "agent_id": str(uuid.uuid4()),
                "call_id": "c1", "session_id": "s1", "turn_id": "t1", "tool_call_id": "tc1",
                "idempotency_key": "idem1", "api_name": "some_api",
                "chain_budget_ms": 5000, "max_chain_depth": 1,
            })
    assert resp.status_code == 200, resp.text
    assert captured["tenant"] == str(two_tenants["b"]["id"])


# =============================================================================
# T58 — full AC 6 matrix, cases 1-3c and 5 (Tier 2)
# =============================================================================
#
# Scope note (lesson 12): the design parameterises cases 1-3c/5 over all 12
# Tier 2 routers; this exercises them fully against one representative
# router — `carriers` (config), a `{tenant_id}` router with NO independent
# app-layer tenant check today besides `require_path_tenant_access` (design
# "Tier 2" table), which is exactly the shape case 3b needs to prove RLS is
# an independent layer rather than a mirror of require_path_tenant_access
# (a router like `agents.py` that ALSO checks the caller itself in its own
# handler would still 404 with require_path_tenant_access removed, proving
# nothing about RLS specifically). Cases 1-3/3c reuse the already-passing
# Tier 2 coverage in test_tier3_carriers's sibling assertions above and in
# test_rls_coverage.py part (b); this block adds the two cases nothing else
# in this file covers: 3b (RLS alone) and 5 (negative control).

from services.config import db as config_db  # noqa: E402


async def test_ac6_case1_superadmin_sees_target_tenants_rows(pool, two_tenants, superadmin):
    with _as_tenant(two_tenants["b"]["id"]):
        carrier_b = await carriers_service.create_carrier(tenant_id=two_tenants["b"]["id"], name="Case1 Carrier", provider="twilio")
    baseline = await pool.fetch("SELECT id FROM carriers WHERE tenant_id = $1", two_tenants["b"]["id"])
    assert {r["id"] for r in baseline} == {carrier_b["id"]}

    async with _client(config_app, superadmin["token"]) as c:
        resp = await c.get(f"/tenants/{two_tenants['b']['id']}/carriers")
    assert resp.status_code == 200
    assert {r["id"] for r in resp.json()} == {str(carrier_b["id"])}


async def test_ac6_case2_superadmin_write_lands_under_target_tenant(two_tenants, pool, superadmin):
    async with _client(config_app, superadmin["token"]) as c:
        resp = await c.post(f"/tenants/{two_tenants['b']['id']}/carriers", json={"name": "Case2 Carrier", "provider": "twilio"})
    assert resp.status_code == 201
    row = await pool.fetchrow("SELECT tenant_id FROM carriers WHERE id = $1", resp.json()["id"])
    assert row["tenant_id"] == two_tenants["b"]["id"]


async def test_ac6_case3_tenant_admin_refused_read_and_write_on_foreign_tenant(two_tenants, superadmin, tenant_admin_a):
    with _as_tenant(two_tenants["b"]["id"]):
        await carriers_service.create_carrier(tenant_id=two_tenants["b"]["id"], name="Case3 Carrier", provider="twilio")

    async with _client(config_app, tenant_admin_a["token"]) as c:
        read_resp = await c.get(f"/tenants/{two_tenants['b']['id']}/carriers")
        write_resp = await c.post(f"/tenants/{two_tenants['b']['id']}/carriers", json={"name": "Hostile", "provider": "twilio"})
    assert read_resp.status_code == 403
    assert write_resp.status_code == 403


async def test_ac6_case3c_leftover_tenant_superadmin_narrowed_to_own_tenant(two_tenants, superadmin_with_tenant_a):
    async with _client(config_app, superadmin_with_tenant_a["token"]) as c:
        foreign = await c.get(f"/tenants/{two_tenants['b']['id']}/carriers")
        own = await c.get(f"/tenants/{two_tenants['a']['id']}/carriers")
    assert foreign.status_code == 403
    assert own.status_code == 200


async def test_ac6_case3b_rls_alone_independent_of_the_app_layer(pool, two_tenants, tenant_admin_a):
    """The one case that distinguishes this design's `target if caller is
    None else caller` reader from the old `target`-wins reader: with
    `require_path_tenant_access` removed AND a real yuviz_app connection
    (not the superuser DSN every other case in this file runs under), a
    tenant-scoped admin of A hitting `/tenants/{B}/carriers` still reads
    zero rows (RLS's USING clause, not the app check) and still can't
    write into B (WITH CHECK). Under a restored `target`-wins reader this
    would incorrectly ADMIT both, because current_tenant() would resolve
    to B for this caller."""
    from services.config import deps as config_deps

    with _as_tenant(two_tenants["b"]["id"]):
        carrier_b = await carriers_service.create_carrier(tenant_id=two_tenants["b"]["id"], name="Case3b Carrier", provider="twilio")

    await pool.execute("ALTER ROLE yuviz_app PASSWORD 'rls-test-only-password'")
    app_pool = await asyncpg.create_pool(_yuviz_app_dsn(), min_size=1, max_size=2)
    config_app.dependency_overrides[config_deps.require_path_tenant_access] = lambda: None
    old_pool = config_db._pool
    config_db._pool = app_pool
    try:
        async with _client(config_app, tenant_admin_a["token"]) as c:
            resp = await c.get(f"/tenants/{two_tenants['b']['id']}/carriers")
        assert resp.status_code == 200
        assert resp.json() == []  # RLS's USING clause, not require_path_tenant_access, emptied this

        with _as_tenant(two_tenants["a"]["id"]):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await carriers_service.create_carrier(
                    tenant_id=two_tenants["b"]["id"], name="Hostile write", provider="twilio",
                )
    finally:
        config_app.dependency_overrides.pop(config_deps.require_path_tenant_access, None)
        config_db._pool = old_pool
        await app_pool.close()
        await pool.execute("DELETE FROM carriers WHERE id = $1", carrier_b["id"])


async def test_ac6_case5_negative_control_bind_path_tenant_is_what_makes_case1_pass(two_tenants, superadmin):
    """Removing `bind_path_tenant` (the dependency case 1 depends on) must
    make case 1 fail — proving the passing case above isn't a false
    positive (lesson 12)."""
    from services.config import deps as config_deps

    with _as_tenant(two_tenants["b"]["id"]):
        await carriers_service.create_carrier(tenant_id=two_tenants["b"]["id"], name="Case5 Carrier", provider="twilio")

    from libs.tenancy import TenantUnresolved

    config_app.dependency_overrides[config_deps.bind_path_tenant] = lambda: None
    try:
        # A NULL-tenant caller with no target set at all is an unresolved
        # scope (AC 9): TenantUnresolved propagates as an unhandled error —
        # never a 200 with B's rows — which is exactly the regression
        # bind_path_tenant exists to prevent (ASGITransport re-raises app
        # exceptions by default rather than turning them into a response,
        # which is what lets this assert the exception directly).
        with pytest.raises(TenantUnresolved):
            async with _client(config_app, superadmin["token"]) as c:
                await c.get(f"/tenants/{two_tenants['b']['id']}/carriers")
    finally:
        config_app.dependency_overrides.pop(config_deps.bind_path_tenant, None)
