"""
campaign_contacts CRUD — the per-contact call queue a campaign works
through. CSV upload is parsed here (not in the router) so the parsing
logic is unit-testable without an HTTP layer.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import db


def parse_contacts_csv(content: bytes) -> list[dict[str, str]]:
    """Expects a header row with at least a 'phone_number' column and an
    optional 'name' column — column order doesn't matter, extra columns
    are ignored. Blank phone_number rows are skipped rather than raising,
    since a hand-edited CSV exported from a spreadsheet often has trailing
    blank rows."""
    text = content.decode("utf-8-sig")  # -sig: strips a BOM Excel-exported CSVs commonly carry
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "phone_number" not in [f.strip().lower() for f in reader.fieldnames]:
        raise ValueError("CSV must have a 'phone_number' column")

    # Normalize header casing once rather than assuming the file used
    # exactly "phone_number"/"name" verbatim.
    field_map = {f.strip().lower(): f for f in reader.fieldnames}
    phone_col = field_map["phone_number"]
    name_col = field_map.get("name")

    contacts = []
    for row in reader:
        phone = (row.get(phone_col) or "").strip()
        if not phone:
            continue
        contacts.append({"phone_number": phone, "name": (row.get(name_col) or "").strip() if name_col else ""})
    return contacts


async def bulk_insert_contacts(
    campaign_id: Any, contacts: list[dict[str, str]], *, platform_scoped: bool = False,
) -> int:
    if not contacts:
        return 0
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        await conn.executemany(
            "INSERT INTO campaign_contacts (campaign_id, phone_number, name) VALUES ($1, $2, $3)",
            [(campaign_id, c["phone_number"], c.get("name") or None) for c in contacts],
        )
    return len(contacts)


async def list_contacts(
    campaign_id: Any, *, status: str | None = None, platform_scoped: bool = False,
) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        if status is not None:
            rows = await conn.fetch(
                "SELECT * FROM campaign_contacts WHERE campaign_id = $1 AND status = $2 ORDER BY created_at",
                campaign_id, status,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM campaign_contacts WHERE campaign_id = $1 ORDER BY created_at", campaign_id,
            )
    return [dict(row) for row in rows]


async def claim_next_pending(campaign_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """Atomically claims one pending contact via SKIP LOCKED so two worker
    ticks never dial the same contact twice."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM campaign_contacts WHERE campaign_id = $1 AND status = 'pending' "
            "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
            campaign_id,
        )
        if row is None:
            return None
        updated = await conn.fetchrow(
            "UPDATE campaign_contacts SET status = 'calling', attempt_count = attempt_count + 1, "
            "last_attempted_at = now() WHERE id = $1 RETURNING *",
            row["id"],
        )
        return dict(updated)


async def mark_contact_status(
    contact_id: Any, status: str, *, call_session_id: str | None = None, platform_scoped: bool = False,
) -> None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        await conn.execute(
            "UPDATE campaign_contacts SET status = $2, call_session_id = COALESCE($3, call_session_id) WHERE id = $1",
            contact_id, status, call_session_id,
        )
