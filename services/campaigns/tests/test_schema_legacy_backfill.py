from __future__ import annotations

import asyncpg

from services.campaigns.tests.schema_db import apply_schema, scratch_database
from services.campaigns.tests.test_schema_attempts import _INSERT_ATTEMPT, _seed


async def _calling_contact(conn, campaign, *, call_session_id: str | None) -> str:
    return await conn.fetchval(
        "INSERT INTO campaign_contacts (campaign_id, phone_number, status, call_session_id) "
        "VALUES ($1, '+14155550101', 'calling', $2) RETURNING id",
        campaign, call_session_id,
    )


async def test_legacy_calling_rows_without_an_attempt_are_resolved_and_live_ones_untouched():
    async with scratch_database() as dsn:
        conn = await asyncpg.connect(dsn)
        try:
            tenant, campaign, live = await _seed(conn)
            await conn.execute("UPDATE campaign_contacts SET status = 'calling' WHERE id = $1", live)
            await conn.execute(_INSERT_ATTEMPT, tenant, campaign, live, 1, f"{live}_1", "pending", None)
            placed = await _calling_contact(conn, campaign, call_session_id="sess-1")
            unplaced = await _calling_contact(conn, campaign, call_session_id=None)

            apply_schema(dsn)

            async def statuses():
                return {r["id"]: r["status"] for r in await conn.fetch("SELECT id, status FROM campaign_contacts")}

            after = await statuses()
            assert after == {live: "calling", placed: "completed", unplaced: "failed"}

            apply_schema(dsn)
            assert await statuses() == after
        finally:
            await conn.close()
