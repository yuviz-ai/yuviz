from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as redis

from libs.tenancy import set_target_tenant  # noqa: E402
from services.campaigns import db  # noqa: E402
from services.campaigns.tests.schema_db import scratch_database  # noqa: E402


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    p = await db.get_pool()
    yield p


@pytest_asyncio.fixture(loop_scope="session")
async def test_tenant(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    row = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Test Tenant {slug}", slug,
    )
    tenant = dict(row)
    yield tenant
    await pool.execute(
        "DELETE FROM campaign_contacts WHERE campaign_id IN (SELECT id FROM campaigns WHERE tenant_id = $1)",
        tenant["id"],
    )
    await pool.execute("DELETE FROM campaigns WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM dnc_numbers WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM agents WHERE tenant_id = $1", tenant["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def test_agent(pool, test_tenant):
    row = await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'test-agent', 'Test Agent') RETURNING *",
        test_tenant["id"],
    )
    return dict(row)


@pytest_asyncio.fixture
async def scoped(test_tenant):
    """Set the ambient RLS tenant scope for direct (non-HTTP) service calls."""
    set_target_tenant(str(test_tenant["id"]))
    yield
    set_target_tenant(None)


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def scratch_pool():
    """Superuser pool on a throwaway database for seeding and inspection; the app itself runs as yuviz_app, so RLS applies."""
    async with scratch_database() as dsn:
        parts = urlsplit(dsn)
        # Roles are cluster-wide and other test modules reset yuviz_app's password, so set it here.
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute("ALTER ROLE yuviz_app PASSWORD 'scratch'")
        finally:
            await admin.close()
        await db.close_pool()
        await db.get_pool(urlunsplit(parts._replace(netloc=f"yuviz_app:scratch@{parts.hostname}:{parts.port or 5432}")))
        p = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        yield p
        await p.close()
        await db.close_pool()


@pytest.fixture(scope="session")
def _scratch_redis_port(tmp_path_factory):
    """A throwaway redis-server without ACLs, so stream and claim assertions never touch the dev instance."""
    if shutil.which("redis-server") is None:
        pytest.skip("BLOCKED-ENV: redis-server not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
         "--dir", str(tmp_path_factory.mktemp("redis"))],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("throwaway redis did not start")
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def _flushed_client(port: int, *, decode_responses: bool):
    client = redis.Redis(port=port, decode_responses=decode_responses)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest_asyncio.fixture(loop_scope="session")
async def scratch_redis(_scratch_redis_port):
    """Decoded-string client on the throwaway Redis, flushed around each test."""
    async for client in _flushed_client(_scratch_redis_port, decode_responses=True):
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def scratch_redis_raw(_scratch_redis_port):
    """Bytes client (what outcome_events requires), flushed around each test."""
    async for client in _flushed_client(_scratch_redis_port, decode_responses=False):
        yield client
