from __future__ import annotations

import asyncpg
import pytest

from services.campaigns.tests.schema_db import apply_schema, scratch_database


async def test_platform_limits_seed_and_cap_fits():
    async with scratch_database() as dsn:
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow("SELECT * FROM platform_outbound_limits")
            assert (row["total_call_capacity"], row["inbound_reserve"], row["max_outbound_concurrent_calls"]) == (300, 75, 225)

            with pytest.raises(asyncpg.CheckViolationError, match="platform_outbound_cap_fits"):
                await conn.execute("UPDATE platform_outbound_limits SET max_outbound_concurrent_calls = 226")

            await conn.execute("UPDATE platform_outbound_limits SET max_outbound_concurrent_calls = 100")
            apply_schema(dsn)
            assert await conn.fetchval("SELECT max_outbound_concurrent_calls FROM platform_outbound_limits") == 100
            assert await conn.fetchval("SELECT count(*) FROM platform_outbound_limits") == 1
        finally:
            await conn.close()


async def test_wait_reason_and_tenant_cap_checks():
    async with scratch_database() as dsn:
        conn = await asyncpg.connect(dsn)
        try:
            tenant = await conn.fetchval("INSERT INTO tenants (name, slug) VALUES ('T', 't1') RETURNING id")
            agent = await conn.fetchval("INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'a', 'A') RETURNING id", tenant)
            campaign = await conn.fetchval(
                "INSERT INTO campaigns (tenant_id, agent_id, name) VALUES ($1, $2, 'C') RETURNING id", tenant, agent,
            )

            await conn.execute("UPDATE campaigns SET wait_reason = 'platform_cap' WHERE id = $1", campaign)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE campaigns SET wait_reason = 'bogus' WHERE id = $1", campaign)

            await conn.execute("UPDATE tenants SET max_outbound_concurrent_calls = 5 WHERE id = $1", tenant)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE tenants SET max_outbound_concurrent_calls = 0 WHERE id = $1", tenant)
        finally:
            await conn.close()
