"""Row builders for tests that run against a scratch database."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from services.campaigns import attempts


async def tenant(conn: asyncpg.Connection, *, outbound_cap: int | None = None) -> uuid.UUID:
    slug = f"t-{uuid.uuid4().hex[:10]}"
    return await conn.fetchval(
        "INSERT INTO tenants (name, slug, max_outbound_concurrent_calls) VALUES ($1, $1, $2) RETURNING id",
        slug, outbound_cap,
    )


async def campaign(
    conn: asyncpg.Connection, tenant_id: uuid.UUID, *, max_attempts: int = 3, max_concurrent_calls: int = 5,
    retry_gap_minutes: int = 60, agent_max_call_s: int | None = None, status: str = "running",
) -> dict[str, Any]:
    agent = await conn.fetchval(
        "INSERT INTO agents (tenant_id, slug, name, max_call_duration_s, direction) VALUES ($1, $2, 'A', $3, 'outbound') RETURNING id",
        tenant_id, f"a-{uuid.uuid4().hex[:8]}", agent_max_call_s,
    )
    row = await conn.fetchrow(
        "INSERT INTO campaigns (tenant_id, agent_id, name, caller_id, max_attempts, max_concurrent_calls, "
        "retry_gap_minutes, status) VALUES ($1, $2, 'C', '+14155550100', $3, $4, $5, $6) RETURNING *",
        tenant_id, agent, max_attempts, max_concurrent_calls, retry_gap_minutes, status,
    )
    return dict(row)


async def contact(
    conn: asyncpg.Connection, campaign_id: uuid.UUID, *, status: str = "pending", attempt_count: int = 0,
    phone: str | None = None,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        "INSERT INTO campaign_contacts (campaign_id, phone_number, status, attempt_count) "
        "VALUES ($1, $2, $3, $4) RETURNING *",
        campaign_id, phone or f"+1415555{uuid.uuid4().int % 10000:04d}", status, attempt_count,
    )
    return dict(row)


async def attempt(
    conn: asyncpg.Connection, camp: dict[str, Any], cont: dict[str, Any], *, n: int = 1, path: str = "sip",
    state: str = "pending", outcome: str | None = None, closed_by_deadline: bool = False,
    provider_call_id: str | None = None, cps_scope: str = "sip_trunk:pstn", last_send_at_ago_s: float | None = None,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        "INSERT INTO campaign_attempts (tenant_id, campaign_id, contact_id, attempt, idempotency_key, path, "
        "state, outcome, closed_by_deadline, provider_call_id, cps_scope, deadline_at, resolved_at, last_send_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now() + interval '2 minutes', "
        "CASE WHEN $8::text IS NULL THEN NULL ELSE now() END, "
        "CASE WHEN $12::float8 IS NULL THEN NULL ELSE clock_timestamp() - make_interval(secs => $12) END) "
        "RETURNING *",
        camp["tenant_id"], camp["id"], cont["id"], n, attempts.idempotency_key(cont["id"], n), path, state,
        outcome, closed_by_deadline, provider_call_id, cps_scope, last_send_at_ago_s,
    )
    return dict(row)
