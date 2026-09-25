"""
dnc.py — per-tenant do-not-call list. Numbers on it are always skipped by
CSV upload (campaign_contacts.py) and re-checked by the worker right
before dialing (defense in depth, in case a number is added to the list
after contacts were already uploaded into a still-running campaign).
"""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import db


def normalize_phone(phone: str) -> str:
    """Same last-10-digits tolerance as
    tools/providers/calendar/cal_com.py's _normalize_phone — a number
    entered as "+1 415-555-0100" and one uploaded as "4155550100" must be
    recognized as the same DNC entry."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


async def add_number(tenant_id: Any, phone_number: str, *, reason: str | None = None) -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO dnc_numbers (tenant_id, phone_number, reason) VALUES ($1, $2, $3) "
            "ON CONFLICT (tenant_id, phone_number) DO UPDATE SET reason = EXCLUDED.reason "
            "RETURNING *",
            tenant_id, phone_number, reason,
        )
    return dict(row)


async def get_dnc_number(dnc_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="dnc-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow("SELECT * FROM dnc_numbers WHERE id = $1", dnc_id)
    return dict(row) if row is not None else None


async def remove_number(dnc_id: Any, *, platform_scoped: bool = False, stamp_tenant: Any = None) -> None:
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="dnc-by-id", stamp_tenant=stamp_tenant)
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        await conn.execute("DELETE FROM dnc_numbers WHERE id = $1", dnc_id)


async def list_numbers(tenant_id: Any, *, platform_scoped: bool = False) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        rows = await conn.fetch(
            "SELECT * FROM dnc_numbers WHERE tenant_id = $1 ORDER BY created_at DESC", tenant_id,
        )
    return [dict(row) for row in rows]


async def is_blocked(tenant_id: Any, phone_number: str, *, platform_scoped: bool = False) -> bool:
    """Loads the tenant's DNC list and matches client-side by normalized digits."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        rows = await conn.fetch("SELECT phone_number FROM dnc_numbers WHERE tenant_id = $1", tenant_id)
    target = normalize_phone(phone_number)
    return any(normalize_phone(row["phone_number"]) == target for row in rows)
