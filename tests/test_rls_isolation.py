"""tests/test_rls_isolation.py — direct-yuviz_app negative suite (T7).

Connects straight to Postgres as yuviz_app, with NO app layer in between:
no tenant_conn(), no deps.py, no FastAPI. This is what proves the database
itself enforces isolation, independent of any Python bug above it (AC 3/4/
5/6/10). Requires database/rls.sql to already be applied against
$POSTGRES_DSN (a local `voiceai` database, same convention as
services/*/tests/conftest.py).

Every "cross-tenant read is empty" case carries a same-tenant counter-check
that reads the row it just inserted: an empty result on a broken fixture
would otherwise pass for the wrong reason (lesson 12).
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

POSTGRES_DSN = os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")
_APP_PASSWORD = "rls-test-only-password"


def _app_dsn() -> str:
    """Swap only the user/password component of POSTGRES_DSN for yuviz_app's."""
    import urllib.parse as up

    parts = up.urlsplit(POSTGRES_DSN)
    netloc = f"yuviz_app:{_APP_PASSWORD}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return up.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture
async def superuser_conn():
    conn = await asyncpg.connect(POSTGRES_DSN)
    # Give yuviz_app a password this test knows, without touching rls.sql's
    # own ALTER ROLE re-assertions of NOSUPERUSER/NOBYPASSRLS.
    await conn.execute(f"ALTER ROLE yuviz_app PASSWORD '{_APP_PASSWORD}'")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def two_tenants(superuser_conn):
    a_id, b_id = uuid.uuid4(), uuid.uuid4()
    a_slug, b_slug = f"rls-a-{a_id.hex[:8]}", f"rls-b-{b_id.hex[:8]}"
    await superuser_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, 'RLS Test A', $2), ($3, 'RLS Test B', $4)",
        a_id, a_slug, b_id, b_slug,
    )
    agent_a, agent_b = uuid.uuid4(), uuid.uuid4()
    await superuser_conn.execute(
        "INSERT INTO agents (id, tenant_id, slug, name) VALUES ($1, $2, 'agent-a', 'Agent A')",
        agent_a, a_id,
    )
    await superuser_conn.execute(
        "INSERT INTO agents (id, tenant_id, slug, name) VALUES ($1, $2, 'agent-b', 'Agent B')",
        agent_b, b_id,
    )
    call_a, call_b = f"rls-call-a-{a_id.hex[:8]}", f"rls-call-b-{b_id.hex[:8]}"
    await superuser_conn.execute(
        "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
        call_a, a_slug,
    )
    await superuser_conn.execute(
        "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, $2, 'inbound')",
        call_b, b_slug,
    )
    try:
        yield {
            "a_id": a_id, "b_id": b_id, "a_slug": a_slug, "b_slug": b_slug,
            "agent_a": agent_a, "agent_b": agent_b, "call_a": call_a, "call_b": call_b,
        }
    finally:
        await superuser_conn.execute("DELETE FROM calls WHERE session_id = ANY($1)", [call_a, call_b])
        await superuser_conn.execute("DELETE FROM agents WHERE id = ANY($1)", [agent_a, agent_b])
        await superuser_conn.execute("DELETE FROM tenants WHERE id = ANY($1)", [a_id, b_id])


@pytest.fixture
async def app_conn():
    conn = await asyncpg.connect(_app_dsn())
    try:
        yield conn
    finally:
        await conn.close()


async def test_cross_tenant_read_is_empty_with_same_tenant_counter_check(two_tenants, app_conn):
    async with app_conn.transaction():
        await app_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(two_tenants["a_id"]))
        # Same-tenant counter-check: proves the fixture and the GUC both
        # work, so the cross-tenant empty result below cannot pass by
        # accident (lesson 12).
        own = await app_conn.fetchrow("SELECT id FROM agents WHERE id = $1", two_tenants["agent_a"])
        assert own is not None

        foreign = await app_conn.fetchrow("SELECT id FROM agents WHERE id = $1", two_tenants["agent_b"])
        assert foreign is None


async def test_no_guc_read_is_empty_not_an_error(two_tenants, app_conn):
    async with app_conn.transaction():
        # No SET LOCAL app.tenant_id at all: NULLIF(current_setting(...), '')
        # is NULL, `NULL = tenant_id` is NULL, so this is zero rows, not a
        # raised `invalid input syntax for type uuid`.
        rows = await app_conn.fetch("SELECT id FROM agents WHERE id = ANY($1)",
                                     [two_tenants["agent_a"], two_tenants["agent_b"]])
        assert rows == []


async def test_cross_tenant_write_rejected(two_tenants, app_conn):
    async with app_conn.transaction():
        await app_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(two_tenants["a_id"]))
        # A row USING already hides (agent_b, foreign) matches zero rows on
        # UPDATE — silent, not an error, and not what WITH CHECK guards.
        # WITH CHECK fires when a VISIBLE row's write would move it out of
        # scope: moving tenant A's own agent into tenant B.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(
                "UPDATE agents SET tenant_id = $2 WHERE id = $1",
                two_tenants["agent_a"], two_tenants["b_id"],
            )


async def test_calls_slug_scoped_read(two_tenants, app_conn):
    async with app_conn.transaction():
        await app_conn.execute("SELECT set_config('app.tenant_slug', $1, true)", two_tenants["a_slug"])
        own = await app_conn.fetchrow(
            "SELECT session_id FROM calls WHERE session_id = $1", two_tenants["call_a"],
        )
        assert own is not None
        foreign = await app_conn.fetchrow(
            "SELECT session_id FROM calls WHERE session_id = $1", two_tenants["call_b"],
        )
        assert foreign is None


async def test_platform_bypass_restores_full_row_set_and_reverts(two_tenants, app_conn, superuser_conn):
    baseline = await superuser_conn.fetch(
        "SELECT id FROM agents WHERE id = ANY($1)", [two_tenants["agent_a"], two_tenants["agent_b"]],
    )
    assert len(baseline) == 2

    async with app_conn.transaction():
        await app_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(two_tenants["a_id"]))
        await app_conn.execute("SET LOCAL ROLE yuviz_platform")
        bypassed = await app_conn.fetch(
            "SELECT id FROM agents WHERE id = ANY($1)", [two_tenants["agent_a"], two_tenants["agent_b"]],
        )
        assert {r["id"] for r in bypassed} == {two_tenants["agent_a"], two_tenants["agent_b"]}

    # SET LOCAL ROLE reverts at transaction end, same as SET LOCAL GUCs — the
    # next transaction on this connection is back under yuviz_app, no bypass.
    async with app_conn.transaction():
        reverted = await app_conn.fetch(
            "SELECT id FROM agents WHERE id = ANY($1)", [two_tenants["agent_a"], two_tenants["agent_b"]],
        )
        assert reverted == []
