"""campaign_contacts CRUD and CSV parsing — the per-contact call queue."""

from __future__ import annotations

import csv
import io
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import db, originate

_PHONE_SEPARATORS = str.maketrans("", "", " -().")


def parse_contacts_csv(content: bytes) -> list[dict[str, str]]:
    """Parse a CSV with a 'phone_number' (and optional 'name') column; blank rows skipped.

    Any invalid number fails the whole upload, naming spreadsheet rows (header = row 1)."""
    text = content.decode("utf-8-sig")  # -sig: strips a BOM Excel-exported CSVs commonly carry
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "phone_number" not in [f.strip().lower() for f in reader.fieldnames]:
        raise ValueError("CSV must have a 'phone_number' column")

    field_map = {f.strip().lower(): f for f in reader.fieldnames}
    phone_col = field_map["phone_number"]
    name_col = field_map.get("name")

    contacts = []
    invalid_rows = []
    for row_num, row in enumerate(reader, start=2):
        phone = (row.get(phone_col) or "").strip()
        if not phone:
            continue
        phone = phone.translate(_PHONE_SEPARATORS)
        if not originate.is_valid_dial_number(phone):
            invalid_rows.append(row_num)
            continue
        contacts.append({"phone_number": phone, "name": (row.get(name_col) or "").strip() if name_col else ""})
    if invalid_rows:
        shown = ", ".join(str(n) for n in invalid_rows[:10])
        more = f" (and {len(invalid_rows) - 10} more)" if len(invalid_rows) > 10 else ""
        raise ValueError(f"invalid phone_number on row(s) {shown}{more}: use digits with an optional leading +")
    return contacts


async def bulk_insert_contacts(
    campaign_id: Any, contacts: list[dict[str, str]], *, platform_scoped: bool = False,
    tenant_id: Any | None = None,
) -> int:
    """tenant_id is the campaign's; it stamps the platform-scoped write."""
    if not contacts:
        return 0
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="campaign-by-id", stamp_tenant=tenant_id) if platform_scoped else tenant_conn(pool)
    )
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


async def release_claim(contact_id: Any, *, platform_scoped: bool = False) -> None:
    """Undo claim_next_pending for a dial that never happened, refunding the attempt."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="campaign-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        await conn.execute(
            "UPDATE campaign_contacts SET status = 'pending', attempt_count = GREATEST(attempt_count - 1, 0) "
            "WHERE id = $1 AND status = 'calling'",
            contact_id,
        )


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
