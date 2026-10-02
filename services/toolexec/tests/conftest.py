from __future__ import annotations

import getpass
import os
import uuid

# Must be set before any services.toolexec import reads them at import time.
os.environ.setdefault("TOOLEXEC_TENANT_SECRET_ROOT", "/tmp/voiceai-toolexec-test-secrets")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)
os.environ.setdefault("POSTGRES_DSN", f"postgresql://{getpass.getuser()}@localhost:5432/voiceai")

if "SECRET_ENCRYPTION_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

os.makedirs(os.environ["TOOLEXEC_TENANT_SECRET_ROOT"], exist_ok=True)

# HMAC key for executor._derive(); resolved lazily, tests may repoint the ref.
os.environ.setdefault("TOOLEXEC_TEST_HMAC_KEY", "dev-only-insecure-hmac-key-do-not-deploy-1")
os.environ.setdefault("TOOLEXEC_ARGS_HMAC_KEY_REF", "env:TOOLEXEC_TEST_HMAC_KEY")

import pytest_asyncio  # noqa: E402

from libs.tenancy import set_target_tenant  # noqa: E402
from services.toolexec import db  # noqa: E402


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await db.get_pool()
    yield p


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_agent(pool):
    """Fresh uniquely-slugged (tenant, agent) per test, cleaned up in FK order."""
    slug = f"toolexec-test-{uuid.uuid4().hex[:8]}"
    tenant = dict(await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Toolexec Test {slug}", slug,
    ))
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"],
    ))
    # Service functions open tenant_conn; the HTTP layer binds this in production.
    set_target_tenant(str(tenant["id"]))
    yield tenant, agent
    set_target_tenant(None)
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
    await pool.execute("UPDATE users SET tenant_id = NULL, deleted_at = coalesce(deleted_at, now()) WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
