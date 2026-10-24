from __future__ import annotations

import asyncpg
import pytest

from services.campaigns.tests.schema_db import apply_schema, scratch_database


async def _seed(conn, *, attempts: int = 3):
    tenant = await conn.fetchval("INSERT INTO tenants (name, slug) VALUES ('T', 't-' || gen_random_uuid()) RETURNING id")
    agent = await conn.fetchval("INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'a', 'A') RETURNING id", tenant)
    campaign = await conn.fetchval(
        "INSERT INTO campaigns (tenant_id, agent_id, name, max_attempts) VALUES ($1, $2, 'C', $3) RETURNING id",
        tenant, agent, attempts,
    )
    contact = await conn.fetchval(
        "INSERT INTO campaign_contacts (campaign_id, phone_number) VALUES ($1, '+14155550100') RETURNING id", campaign,
    )
    return tenant, campaign, contact


_INSERT_ATTEMPT = """
INSERT INTO campaign_attempts
    (tenant_id, campaign_id, contact_id, attempt, idempotency_key, path, state, outcome, deadline_at)
VALUES ($1, $2, $3, $4, $5, 'sip', $6, $7, now() + interval '2 minutes')
"""


async def test_reapply_with_legacy_campaign_changes_no_row():
    async with scratch_database() as dsn:
        conn = await asyncpg.connect(dsn)
        try:
            tenant, campaign, contact = await _seed(conn, attempts=1)
            # Recreate the pre-change shape: a max_attempts=0 campaign and a 'calling' contact that has a live attempt.
            await conn.execute("ALTER TABLE campaigns DROP CONSTRAINT campaigns_max_attempts_check")
            await conn.execute("UPDATE campaigns SET max_attempts = 0 WHERE id = $1", campaign)
            await conn.execute("UPDATE campaign_contacts SET status = 'calling' WHERE id = $1", contact)
            await conn.execute(_INSERT_ATTEMPT, tenant, campaign, contact, 1, f"{contact}_1", "pending", None)

            async def snapshot():
                return [
                    [dict(r) for r in await conn.fetch(f"SELECT * FROM {t} ORDER BY id")]
                    for t in ("campaigns", "campaign_contacts", "tenants")
                ]

            before = await snapshot()
            apply_schema(dsn)
            apply_schema(dsn)
            after = await snapshot()
            assert after == before
            assert await conn.fetchval("SELECT max_attempts FROM campaigns WHERE id = $1", campaign) == 0
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE campaigns SET max_attempts = 0, name = 'x' WHERE id = $1", campaign)
        finally:
            await conn.close()


async def test_attempt_constraints():
    async with scratch_database() as dsn:
        conn = await asyncpg.connect(dsn)
        try:
            tenant_a, campaign_a, contact_a = await _seed(conn)
            tenant_b, _, _ = await _seed(conn)

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(_INSERT_ATTEMPT, tenant_b, campaign_a, contact_a, 1, "k-cross", "pending", None)

            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(_INSERT_ATTEMPT, tenant_a, campaign_a, contact_a, 1, "k-bad1", "resolved", None)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(_INSERT_ATTEMPT, tenant_a, campaign_a, contact_a, 1, "k-bad2", "pending", "busy")

            await conn.execute(_INSERT_ATTEMPT, tenant_a, campaign_a, contact_a, 1, "k-ok", "resolved", "busy")
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(_INSERT_ATTEMPT, tenant_a, campaign_a, contact_a, 1, "k-dup", "pending", None)
        finally:
            await conn.close()
