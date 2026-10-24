"""Outbound capacity: the admission check run inside claim_next_due, and limit read/write validation.

Every figure is derived from persisted campaign_attempts and read in the caller's transaction, so a
limit change applies on the next tick and a restart rebuilds usage exactly."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

import asyncpg

WaitReason = Literal[
    "campaign_cap", "tenant_cap", "platform_cap", "trunk_cps", "provider_cps", "calling_hours", "pacing",
    "registry", "insufficient_credit", "budget_reached", "billing_unavailable",
]

# Same predicate as the Path A live-call signal: unresolved, or answered and not yet ended.
IN_FLIGHT = "(state IN ('pending', 'sent') OR outcome = 'answered')"

RECENT_SEND = "last_send_at > clock_timestamp() - interval '1 second'"

SIP_TRUNK_SCOPE = "sip_trunk:pstn"

_CPS_SCOPE = re.compile(r"^(sip_trunk:[a-z0-9_-]{1,40}|provider_account:[0-9a-f-]{36})$")


def provider_scope(telephony_config_id: Any) -> str:
    return f"provider_account:{telephony_config_id}"


class LimitError(ValueError):
    """A limit write that would break an invariant; the message is safe to show the caller."""


async def admit(conn: asyncpg.Connection, campaign: dict[str, Any], cps_scope: str) -> WaitReason | None:
    """First applicable wait reason, or None to admit. Caller holds the admission lock and the campaign row."""
    limits = await conn.fetchrow(
        """
        SELECT c.max_concurrent_calls AS campaign_cap, t.max_outbound_concurrent_calls AS tenant_cap,
               (SELECT max_outbound_concurrent_calls FROM platform_outbound_limits) AS platform_cap,
               (SELECT cps FROM outbound_cps_limits WHERE scope = $3) AS cps
        FROM campaigns c JOIN tenants t ON t.id = c.tenant_id
        WHERE c.id = $1 AND c.tenant_id = $2
        """,
        campaign["id"], campaign["tenant_id"], cps_scope,
    )
    usage = await conn.fetchrow(
        f"""
        SELECT count(*) FILTER (WHERE campaign_id = $1) AS campaign,
               count(*) FILTER (WHERE tenant_id = $2) AS tenant,
               count(*) AS platform
        FROM campaign_attempts WHERE {IN_FLIGHT}
        """,
        campaign["id"], campaign["tenant_id"],
    )
    if usage["campaign"] >= limits["campaign_cap"]:
        return "campaign_cap"
    if limits["tenant_cap"] is not None and usage["tenant"] >= limits["tenant_cap"]:
        return "tenant_cap"
    if limits["platform_cap"] is not None and usage["platform"] >= limits["platform_cap"]:
        return "platform_cap"
    if limits["cps"] is not None and await _recent_sends(conn, cps_scope) >= limits["cps"]:
        return "trunk_cps" if cps_scope.startswith("sip_trunk:") else "provider_cps"
    return None


async def cps_exhausted(conn: asyncpg.Connection, cps_scope: str) -> bool:
    """True when the scope has a CPS limit and its one-second window is full (re-send gate)."""
    cps = await conn.fetchval("SELECT cps FROM outbound_cps_limits WHERE scope = $1", cps_scope)
    return cps is not None and await _recent_sends(conn, cps_scope) >= cps


async def _recent_sends(conn: asyncpg.Connection, cps_scope: str) -> int:
    return await conn.fetchval(
        f"SELECT count(*) FROM campaign_attempts WHERE cps_scope = $1 AND {RECENT_SEND}",
        cps_scope,
    )


async def write_wait_reason(conn: asyncpg.Connection, campaign_id: UUID, reason: WaitReason | None) -> None:
    """Touches the campaign row only when the reason changes."""
    await conn.execute(
        "UPDATE campaigns SET wait_reason = $2, wait_since = CASE WHEN $2::text IS NULL THEN NULL ELSE now() END "
        "WHERE id = $1 AND wait_reason IS DISTINCT FROM $2",
        campaign_id, reason,
    )


async def get_platform_limits(conn: asyncpg.Connection, *, for_update: bool = False) -> dict[str, Any]:
    return dict(await conn.fetchrow("SELECT * FROM platform_outbound_limits" + (" FOR UPDATE" if for_update else "")))


async def list_cps_limits(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch("SELECT scope, cps, updated_at FROM outbound_cps_limits ORDER BY scope")]


async def get_tenant_cap(conn: asyncpg.Connection, tenant_id: UUID, *, for_update: bool = False) -> dict[str, Any] | None:
    """The tenant's cap and live in-flight count, or None when the tenant does not exist."""
    row = await conn.fetchrow(
        f"SELECT max_outbound_concurrent_calls, (SELECT count(*) FROM campaign_attempts WHERE tenant_id = $1 AND {IN_FLIGHT}) "
        "AS in_flight FROM tenants WHERE id = $1" + (" FOR UPDATE" if for_update else ""),
        tenant_id,
    )
    return dict(row) if row is not None else None


async def list_tenant_caps(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT t.id AS tenant_id, t.name, t.slug, t.max_outbound_concurrent_calls, coalesce(f.n, 0) AS in_flight
        FROM tenants t
        LEFT JOIN (SELECT tenant_id, count(*) AS n FROM campaign_attempts WHERE {IN_FLIGHT} GROUP BY tenant_id) f
               ON f.tenant_id = t.id
        ORDER BY t.name, t.slug
        """
    )
    return [dict(r) for r in rows]


async def usage(conn: asyncpg.Connection) -> dict[str, Any]:
    """Live counts from the same predicates admission uses; every configured scope plus any with sends in the window."""
    in_flight = await conn.fetchval(f"SELECT count(*) FROM campaign_attempts WHERE {IN_FLIGHT}")
    scopes = await conn.fetch(
        f"""
        SELECT coalesce(l.scope, s.scope) AS scope, l.cps, coalesce(s.n, 0) AS sends_last_second
        FROM outbound_cps_limits l
        FULL JOIN (SELECT cps_scope AS scope, count(*) AS n FROM campaign_attempts
                   WHERE {RECENT_SEND} GROUP BY cps_scope) s ON s.scope = l.scope
        ORDER BY 1
        """
    )
    return {"platform_in_flight": in_flight, "cps": [dict(r) for r in scopes]}


async def set_platform_limits(
    conn: asyncpg.Connection, *, total_call_capacity: int | None, inbound_reserve: int,
    max_outbound_concurrent_calls: int | None, updated_by: UUID | None = None,
) -> dict[str, Any]:
    if max_outbound_concurrent_calls is not None and (
        total_call_capacity is None or max_outbound_concurrent_calls > total_call_capacity - inbound_reserve
    ):
        raise LimitError("outbound cap exceeds capacity minus inbound reserve")
    try:
        row = await conn.fetchrow(
            "UPDATE platform_outbound_limits SET total_call_capacity = $1, inbound_reserve = $2, "
            "max_outbound_concurrent_calls = $3, updated_at = now(), updated_by = $4 RETURNING *",
            total_call_capacity, inbound_reserve, max_outbound_concurrent_calls, updated_by,
        )
    except asyncpg.CheckViolationError as exc:
        raise LimitError("limits must be positive and the reserve non-negative") from exc
    return dict(row)


async def set_tenant_cap(conn: asyncpg.Connection, tenant_id: UUID, cap: int | None) -> bool:
    """False when the tenant does not exist."""
    if cap is not None and cap < 1:
        raise LimitError("tenant outbound cap must be at least 1")
    updated = await conn.fetchval(
        "UPDATE tenants SET max_outbound_concurrent_calls = $2 WHERE id = $1 RETURNING id", tenant_id, cap,
    )
    return updated is not None


async def set_cps_limit(conn: asyncpg.Connection, scope: str, cps: int, updated_by: UUID | None = None) -> None:
    if not _CPS_SCOPE.fullmatch(scope):
        raise LimitError("unknown CPS scope")
    if cps < 1:
        raise LimitError("cps must be at least 1")
    await conn.execute(
        "INSERT INTO outbound_cps_limits (scope, cps, updated_by) VALUES ($1, $2, $3) "
        "ON CONFLICT (scope) DO UPDATE SET cps = $2, updated_by = $3, updated_at = now()",
        scope, cps, updated_by,
    )
