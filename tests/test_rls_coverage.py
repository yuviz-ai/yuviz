"""Mechanical RLS coverage tripwires: table policies, route tenant checks,
tenant_conn reason literals, and tenant-safe cache keys.
Requires database/rls.sql applied against $POSTGRES_DSN.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import pathlib
import re

import asyncpg
import pytest

POSTGRES_DSN = os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")

# Normally set by each service's conftest; this file imports all six apps.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)
os.environ.setdefault("TOOLEXEC_TENANT_SECRET_ROOT", "/tmp/voiceai-toolexec-test-secrets")
os.environ.setdefault("KNOWLEDGE_STORAGE_ROOT", "/tmp/voiceai-knowledge-test-storage")
os.environ.setdefault("TOOLEXEC_TEST_HMAC_KEY", "dev-only-insecure-hmac-key-do-not-deploy-1")
os.environ.setdefault("TOOLEXEC_ARGS_HMAC_KEY_REF", "env:TOOLEXEC_TEST_HMAC_KEY")
os.environ.setdefault("TELEPHONY_PUBLIC_BASE_URL", "https://test.example.com")
os.makedirs(os.environ["TOOLEXEC_TENANT_SECRET_ROOT"], exist_ok=True)
if "SECRET_ENCRYPTION_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Child tables with no tenant_id column, scoped via a parent-join policy.
WAVE_B_TABLES = {
    "agent_tool_policies",
    "agent_workflow_versions",
    "campaign_contacts",
    "custom_api_params",
    "agent_custom_apis",
    "api_chain_steps",
    "transcript_entries",
    "agent_knowledge_bases",
    "agent_retrieval_policies",
    "kb_ingestion_jobs",
}

# Genuinely out of RLS scope, stated so this test doesn't flag them.
OUT_OF_SCOPE_TABLES = {"tenants", "conversation_node_heartbeats", "kamailio_cdr"}


@pytest.fixture
async def conn():
    c = await asyncpg.connect(POSTGRES_DSN)
    try:
        yield c
    finally:
        await c.close()


async def _tables_with_tenant_id_column(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
    )
    return {r["table_name"] for r in rows}


async def _rls_state(conn: asyncpg.Connection, table: str) -> tuple[bool, bool]:
    row = await conn.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = $1 AND relnamespace = 'public'::regnamespace",
        table,
    )
    assert row is not None, f"table {table!r} does not exist"
    return row["relrowsecurity"], row["relforcerowsecurity"]


async def _policy_count(conn: asyncpg.Connection, table: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = $1", table,
    )


async def test_every_tenant_id_column_table_has_rls_force_and_a_policy(conn):
    tables = await _tables_with_tenant_id_column(conn)
    assert tables, "no table with a tenant_id column found — is rls.sql applied against this DB?"

    failures = []
    for table in sorted(tables):
        enabled, forced = await _rls_state(conn, table)
        policies = await _policy_count(conn, table)
        if not (enabled and forced and policies >= 1):
            failures.append((table, enabled, forced, policies))
    assert failures == [], (
        "tables with a tenant_id column missing RLS/FORCE/policy "
        f"(table, relrowsecurity, relforcerowsecurity, policy_count): {failures}"
    )


async def test_wave_b_child_tables_have_rls_force_and_a_policy(conn):
    failures = []
    for table in sorted(WAVE_B_TABLES):
        enabled, forced = await _rls_state(conn, table)
        policies = await _policy_count(conn, table)
        if not (enabled and forced and policies >= 1):
            failures.append((table, enabled, forced, policies))
    assert failures == [], (
        "Wave B tables missing RLS/FORCE/policy "
        f"(table, relrowsecurity, relforcerowsecurity, policy_count): {failures}"
    )


def _expand(route):
    """Yield concrete APIRoutes, recursing into FastAPI's `.original_router` wrappers."""
    original_router = getattr(route, "original_router", None)
    if original_router is not None:
        for sub in original_router.routes:
            yield from _expand(sub)
    elif hasattr(route, "path") and hasattr(route, "dependant") and hasattr(route, "methods"):
        yield route


def _iter_api_routes(app):
    for route in app.routes:
        yield from _expand(route)


def _all_apps():
    """All six FastAPI apps in the repo."""
    from services.campaigns.app import app as campaigns_app
    from services.config.app import app as config_app
    from services.did.app import app as did_app
    from services.knowledge.app import app as knowledge_app
    from services.telephony.app import app as telephony_app
    from services.toolexec.app import app as toolexec_app

    return [config_app, campaigns_app, knowledge_app, toolexec_app, did_app, telephony_app]


# The tenants table is what tenant_conn() itself resolves from, so it's outside RLS.
_OUT_OF_SCOPE_MODULES = {"services.config.routers.tenants"}


def test_every_tenants_path_route_has_bind_and_require_path_tenant_access():
    from services.config.deps import bind_path_tenant, require_path_tenant_access

    failures = []
    for app in _all_apps():
        for route in _iter_api_routes(app):
            if "/tenants/{" not in route.path:
                continue
            if route.endpoint.__module__ in _OUT_OF_SCOPE_MODULES:
                continue
            deps_seen = {d.call for d in route.dependant.dependencies}
            missing = {bind_path_tenant, require_path_tenant_access} - deps_seen
            if missing:
                failures.append((app.title, route.path, tuple(route.methods), [f.__name__ for f in missing]))
    assert failures == [], (
        f"/tenants/{{...}} routes missing bind_path_tenant/require_path_tenant_access: {failures}"
    )


# Flat by-id router -> (sibling modules holding its auth, marker substrings proving a tenant check).
_TIER3_MODULES = {
    "services.config.routers.provider_configs": ([], {"assert_tenant_access"}),
    "services.config.routers.telephony_configs": ([], {"assert_tenant_access"}),
    "services.config.routers.carriers": ([], {"assert_tenant_access"}),
    "services.config.routers.phone_numbers": ([], {"assert_tenant_access"}),
    "services.config.routers.tool_provider_configs": ([], {"assert_tenant_access"}),
    "services.config.routers.calls": ([], {"_caller_tenant_slug"}),
    "services.config.routers.agent_tool_policies": ([], {"assert_tenant_access"}),
    "services.config.routers.users": ([], {"assert_tenant_access"}),
    "services.config.routers.invites": ([], {"assert_tenant_access"}),
    "services.config.routers.live_calls": ([], {"set_target_tenant"}),
    "services.config.routers.call_flows": ([], {"_authorize_flow", "assert_tenant_access"}),
    "services.campaigns.routers.campaigns": ([], {"assert_tenant_access"}),
    "services.knowledge.routers.knowledge_bases": ([], {"_authorize_kb"}),
    "services.knowledge.routers.documents": ([], {"assert_tenant_access"}),
    "services.knowledge.routers.agent_kb": ([], {"assert_tenant_access"}),
    "services.toolexec.routers.custom_apis": ([], {"_authorize_custom_api", "assert_tenant_access"}),
    "services.toolexec.routers.agent_apis": (["services.toolexec.agent_apis"], {"_authorize_agent_api"}),
    "services.did.routers.numbers": ([], {"assert_tenant_access"}),
    # Outbound routes check via auth.resolve_caller_tenant; inbound webhooks are
    # gated by vendor signature only, which this module-level check can't distinguish.
    "services.telephony.app": (["services.telephony.auth"], {"assert_tenant_access"}),
}

# Tier 4 path-tenant routes must call set_target_tenant. Body-tenant Tier 4
# routes have no path param; test_cross_tenant_admin.py covers them.
_TIER4_PATHS = {"/internal/agents/{tenant_slug}/{agent_slug}/has-knowledge"}
_TIER4_MARKER = "set_target_tenant"

# Known exceptions: chain_runs authorizes via agent_apis._authorize_chain_runs;
# retrieval_policies has no tenant check yet.
_PRE_EXISTING_OUT_OF_SCOPE_MODULES = {
    "services.toolexec.routers.chain_runs",
    "services.knowledge.routers.retrieval_policies",
}


def _module_source_with_siblings(module_name: str, sibling_names: list[str]) -> str:
    src = inspect.getsource(importlib.import_module(module_name))
    for sib in sibling_names:
        src += "\n" + inspect.getsource(importlib.import_module(sib))
    return src


def test_every_flat_by_id_route_is_tier3_or_tier4():
    failures = []
    for app in _all_apps():
        for route in _iter_api_routes(app):
            if "/tenants/{" in route.path or "{" not in route.path:
                continue
            module_name = route.endpoint.__module__
            if module_name in _PRE_EXISTING_OUT_OF_SCOPE_MODULES:
                continue
            if route.path in _TIER4_PATHS:
                src = inspect.getsource(route.endpoint)
                if _TIER4_MARKER not in src:
                    failures.append((app.title, route.path, "Tier 4 route missing set_target_tenant"))
                continue
            if module_name not in _TIER3_MODULES:
                failures.append((app.title, route.path, f"module {module_name!r} not on the Tier 3 list"))
                continue
            siblings, markers = _TIER3_MODULES[module_name]
            src = _module_source_with_siblings(module_name, siblings)
            if not any(marker in src for marker in markers):
                failures.append((app.title, route.path, f"no tenant-check marker {markers} found in {module_name}"))
    assert failures == [], f"flat by-id routes failing Tier 3/Tier 4 coverage: {failures}"


_EXPLICIT_OVERRIDE_REASONS = {
    "conversation-session-write",
    "conversation-tool-policy",
    "kb-ingestion-job",
}

# Every platform_conn(reason=...) literal; a new bypass must be added here.
_BYPASS_REASONS = {
    "pre-auth-login", "pre-auth-bootstrap", "pre-auth-register", "pre-auth-invite-accept",
    "invites-create-null-tenant", "invites-null-tenant-listing", "invites-by-id",
    "identity-resolution", "users-null-tenant-listing", "users-admin-by-id",
    "users-create", "users-change-password", "users-email-change",
    "tenants-out-of-rls-scope", "phone-numbers-prewarm",
    "agents-by-id", "call-flow-by-id", "carriers-by-id", "phone-numbers-by-id", "phone-numbers-did-lookup",
    "provider-configs-by-id", "telephony-configs-by-id", "tool-provider-configs-by-id",
    "calls-platform-read", "audit-log-platform-read",
    "campaign-by-id", "dnc-by-id", "campaign-worker-scan",
    "kb-by-id", "kb-admin-by-id", "agent-kb-by-id", "kb-ingestion-job-claim",
    "document-by-id", "document-admin-by-id",
    "custom-apis-admin-by-id", "custom-apis-admin-mutation",
    "agent-apis-admin-by-id", "agent-apis-admin-mutation", "agent-apis-chain-runs",
    "did-purchased-number-by-id",
    "conversation-reconcile-sweep",
    "telephony-account-preload",
    "pre-auth-password-reset",
    "telephony-config-kind", "number-sync-provider-ids", "phone-numbers-provider-sync",
    # The one deliberately cross-tenant READ in the codebase: disconnect asks
    # whether another tenant still holds the same upstream grant, so it can
    # skip the revoke instead of cutting them off. It is a bypass because the
    # question is by definition about rows outside the caller's tenant. What
    # keeps it safe is that the subject it matches on comes from the
    # disconnecting row itself (services/toolexec/oauth.py:590), never from a
    # request field, and the only thing that escapes the connection is a
    # boolean the caller never sees — disconnect returns a constant body.
    "oauth-shared-grant-check",
}

# Platform-branch mutations with genuinely no tenant to stamp.
_UNSTAMPED_MUTATION_EXEMPT_REASONS = {
    "identity-resolution", "users-null-tenant-listing",
    "pre-auth-login", "pre-auth-bootstrap", "pre-auth-register", "pre-auth-invite-accept",
    "invites-create-null-tenant", "invites-null-tenant-listing",
    "tenants-out-of-rls-scope", "phone-numbers-prewarm",
    "campaign-worker-scan", "conversation-reconcile-sweep",
    "kb-ingestion-job-claim", "agent-apis-chain-runs",
    "calls-platform-read", "audit-log-platform-read",
}

_MUTATION_SQL_RE = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM)\b", re.I)
_TENANT_CONN_CALL_NAMES = {"tenant_conn", "platform_conn"}


def _iter_production_py_files():
    for root in (REPO_ROOT / "services", REPO_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            if "/tests/" in str(path):
                continue
            yield path


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _reason_literal(call: ast.Call) -> object:
    for kw in call.keywords:
        if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _has_stamp_tenant(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "stamp_tenant":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


def _collect_tenant_conn_calls():
    """(path, func, call_name, reason, has_stamp_tenant, is_mutation) per tenant_conn/platform_conn call."""
    findings = []
    for path in _iter_production_py_files():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            func_src = ast.get_source_segment(path.read_text(), func) or ""
            is_mutation = bool(_MUTATION_SQL_RE.search(func_src))
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and _call_name(node) in _TENANT_CONN_CALL_NAMES:
                    findings.append((
                        str(path), func.name, _call_name(node),
                        _reason_literal(node), _has_stamp_tenant(node), is_mutation,
                    ))
    return findings


def test_every_reason_literal_is_an_enumerated_bypass_or_override():
    calls = _collect_tenant_conn_calls()
    seen_reasons = {r for (_, _, _, r, _, _) in calls if r is not None}
    expected = _BYPASS_REASONS | _EXPLICIT_OVERRIDE_REASONS
    assert seen_reasons == expected, (
        f"reason= literals not matching the enumerated tables — "
        f"unexpected: {seen_reasons - expected}, missing: {expected - seen_reasons}"
    )


def test_every_platform_conn_mutation_stamps_its_tenant():
    calls = _collect_tenant_conn_calls()
    failures = [
        (path, func, reason)
        for (path, func, call_name, reason, has_stamp, is_mutation) in calls
        if call_name == "platform_conn" and is_mutation and not has_stamp
        and reason not in _UNSTAMPED_MUTATION_EXEMPT_REASONS
    ]
    assert failures == [], f"platform_conn mutations missing stamp_tenant=: {failures}"


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _is_tenant_safe_cache_key(key: str, *, tenant_prefix: str | None = None) -> bool:
    """True if the key embeds a UUID or the tenant's slug prefix (can't collide across tenants)."""
    if _UUID_RE.search(key):
        return True
    if tenant_prefix is not None and (key.startswith(f"{tenant_prefix}:") or f":{tenant_prefix}:" in key):
        return True
    return False


def test_provider_config_cache_key_is_uuid_bearing():
    import uuid as _uuid

    from services.config.provider_configs import _cache_key

    assert _is_tenant_safe_cache_key(_cache_key(_uuid.uuid4()))


def test_telephony_config_cache_key_is_uuid_bearing():
    import uuid as _uuid

    from services.config.telephony_configs import _cache_key

    assert _is_tenant_safe_cache_key(_cache_key(_uuid.uuid4()))


def test_agent_cache_key_is_tenant_prefixed():
    from services.config.agents import cache_key

    key = cache_key("acme-tenant", "sales-bot")
    assert _is_tenant_safe_cache_key(key, tenant_prefix="acme-tenant")


def test_phone_number_cache_key_is_globally_unique_by_construction():
    # An E.164 number can't belong to two tenants, so did:{e164} is safe.
    from services.config.phone_numbers import _cache_key

    assert _cache_key("+15551234567") == "did:+15551234567"


def test_a_bare_slug_keyed_cache_key_fails_the_classifier():
    assert not _is_tenant_safe_cache_key("kb:acme-docs")
