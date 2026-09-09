from __future__ import annotations

import getpass
import os
import uuid

# Set before any services.toolexec module import reads them at import time
# (auth_schemes.TENANT_SECRET_ROOT is read the same fail-loud way
# services/config/auth.py reads JWT_SECRET) — same pattern
# services/knowledge/tests/conftest.py already uses for its own env vars.
os.environ.setdefault("TOOLEXEC_TENANT_SECRET_ROOT", "/tmp/voiceai-toolexec-test-secrets")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)
# Same local dev Postgres every other service's tests point at (docs/setup.md) —
# schema.sql's toolexec tables are additive, so running against it is safe.
os.environ.setdefault("POSTGRES_DSN", f"postgresql://{getpass.getuser()}@localhost:5432/voiceai")

if "SECRET_ENCRYPTION_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

os.makedirs(os.environ["TOOLEXEC_TENANT_SECRET_ROOT"], exist_ok=True)

# The platform secret executor.py's _derive() is keyed by — resolved lazily
# on first use (see executor._get_hmac_key), not at import time, so setting
# this here is enough; individual tests override TOOLEXEC_ARGS_HMAC_KEY_REF
# to point at a SECOND env var when they need a genuinely different key.
os.environ.setdefault("TOOLEXEC_TEST_HMAC_KEY", "dev-only-insecure-hmac-key-do-not-deploy-1")
os.environ.setdefault("TOOLEXEC_ARGS_HMAC_KEY_REF", "env:TOOLEXEC_TEST_HMAC_KEY")

import pytest_asyncio  # noqa: E402

from services.toolexec import db  # noqa: E402


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await db.get_pool()
    yield p


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_agent(pool):
    """A fresh (tenant, agent) pair per test, uniquely slugged so parallel
    test runs never collide on tenants_slug_key. Cleaned up afterward in
    FK-dependency order (deepest tables first)."""
    slug = f"toolexec-test-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Toolexec Test {slug}", slug,
    ))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"],
    ))
    yield tenant, agent
    await pool.execute("DELETE FROM api_side_effect_claims WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM api_chain_steps WHERE run_id IN "
                        "(SELECT id FROM api_chain_runs WHERE tenant_id = $1)", tenant["id"])
    await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agent_custom_apis WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM custom_api_params WHERE custom_api_id IN "
                        "(SELECT id FROM custom_apis WHERE tenant_id = $1)", tenant["id"])
    await pool.execute("DELETE FROM custom_apis WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agent_tool_policies WHERE agent_id = $1", agent["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
