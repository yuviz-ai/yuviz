"""Phone number (DID) CRUD with cache-aside reads and audited mutations.

get_by_did() returns the denormalized route the Gateway reads from Redis on every
inbound call; everything else operates on raw phone_numbers rows.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, db

log = logging.getLogger(__name__)

_UPDATABLE_FIELDS = {"did", "agent_id", "fallback_agent_id", "carrier_id", "telephony_config_id", "region", "status"}

# did:{did} routes have no TTL: the Gateway never falls back to Postgres on a miss,
# so any expiry misroutes calls. Every write path updates Redis; prewarm() covers a flushed Redis.


async def _lock_live_telephony_config(conn: Any, telephony_config_id: Any) -> None:
    """FOR SHARE conflicts with a config delete's FOR UPDATE, so a number
    can't land on a config deleted under it."""
    if telephony_config_id is None:
        return
    found = await conn.fetchval(
        "SELECT 1 FROM telephony_configs WHERE id = $1 AND deleted_at IS NULL FOR SHARE", telephony_config_id,
    )
    if found is None:
        raise LookupError(f"telephony_config {telephony_config_id} not found")


def _cache_key(did: str) -> str:
    return f"did:{did}"


async def get_by_did(did: str, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """`platform_scoped=True` only for prewarm(), which has no request tenant."""
    cached = await cache.get_json(_cache_key(did))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="phone-numbers-did-lookup")
        if platform_scoped
        else tenant_conn(pool)
    )
    # Inactive numbers route like unknown DIDs. Agent falls back primary -> fallback -> 'default',
    # matching the Gateway's PhoneRoute::from_redis().
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT t.slug AS tenant_slug, "
            "       COALESCE(a.slug, fb.slug, 'default') AS agent_slug, "
            "       COALESCE(a.config_version, fb.config_version) AS agent_config_version "
            "FROM phone_numbers pn "
            "JOIN tenants t ON t.id = pn.tenant_id "
            "LEFT JOIN agents a  ON a.id = pn.agent_id AND a.deleted_at IS NULL AND a.status = 'active' "
            "LEFT JOIN agents fb ON fb.id = pn.fallback_agent_id AND fb.deleted_at IS NULL AND fb.status = 'active' "
            "WHERE pn.did = $1 AND pn.status = 'active' "
            "  AND pn.deleted_at IS NULL AND t.deleted_at IS NULL",
            did,
        )
    if row is None:
        return None

    result = {
        "tenant_slug": row["tenant_slug"],
        "agent_slug": row["agent_slug"],
        # null when agent_slug fell through to the literal 'default'.
        "version": row["agent_config_version"],
    }
    await cache.set_json(_cache_key(did), result, ttl=None)
    return result


async def prewarm() -> int:
    """Populate did:{did} for every active number at startup; returns the count warmed."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="phone-numbers-prewarm") as conn:
        rows = await conn.fetch(
            "SELECT did FROM phone_numbers WHERE status = 'active' AND deleted_at IS NULL",
        )
    for row in rows:
        await get_by_did(row["did"], platform_scoped=True)
    return len(rows)


async def get_phone_number(phone_number_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="phone-numbers-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM phone_numbers WHERE id = $1 AND deleted_at IS NULL", phone_number_id,
        )
    return _row(row) if row is not None else None


async def list_phone_numbers(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM phone_numbers WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY did",
            tenant_id,
        )
    return [_row(row) for row in rows]


def _row(row: Any) -> dict[str, Any]:
    result = dict(row)
    if "provider_sync" in result:
        result["provider_sync"] = db.json_col(result["provider_sync"])
    return result


async def record_provider_sync(phone_number_id: Any, tenant_id: Any, sync: dict[str, Any]) -> dict[str, Any]:
    """Stores the latest sync outcome on the number; returns it with its time."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="phone-numbers-provider-sync", stamp_tenant=str(tenant_id)) as conn:
        stored = await conn.fetchval(
            "UPDATE phone_numbers SET provider_sync = jsonb_build_object("
            "  'ok', $2::boolean, 'message', $3::text, 'at', to_jsonb(now())"
            ") WHERE id = $1 AND tenant_id = $4 RETURNING provider_sync",
            phone_number_id, sync["ok"], sync.get("message"), tenant_id,
        )
    return db.json_col(stored)


class DidAlreadyAssigned(Exception):
    """The DID has a live row (any tenant). Deliberately names no owner: a
    tenant probing a number must not learn which customer holds it."""

    def __init__(self, did: str) -> None:
        self.did = did
        super().__init__(f"{did} is already assigned")


def _raise_if_did_taken(exc: asyncpg.UniqueViolationError, did: str) -> None:
    if exc.constraint_name == "phone_numbers_did_live_key":
        raise DidAlreadyAssigned(did) from None


async def create_phone_number(
    *,
    tenant_id: Any,
    did: str,
    agent_id: Any | None = None,
    fallback_agent_id: Any | None = None,
    carrier_id: Any | None = None,
    telephony_config_id: Any | None = None,
    region: str | None = None,
    status: str = "active",
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await _lock_live_telephony_config(conn, telephony_config_id)
        try:
            row = await conn.fetchrow(
                "INSERT INTO phone_numbers "
                "(tenant_id, did, agent_id, fallback_agent_id, carrier_id, telephony_config_id, region, status) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
                tenant_id, did, agent_id, fallback_agent_id, carrier_id, telephony_config_id, region, status,
            )
        except asyncpg.UniqueViolationError as exc:
            _raise_if_did_taken(exc, did)
            raise
        result = _row(row)
        await audit.write_audit(
            conn,
            entity_type="phone_number",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value=result,
        )
    # Invalidate first: a re-added DID may still have its previous owner's route cached.
    await cache.invalidate(_cache_key(did))
    await get_by_did(did)
    return result


async def update_phone_number(
    phone_number_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_phone_number() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_phone_number() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # FOR UPDATE so a concurrent update can't make the audit old_value stale.
        old_row = await conn.fetchrow(
            "SELECT * FROM phone_numbers WHERE id = $1 FOR UPDATE", phone_number_id,
        )
        if old_row is None:
            raise LookupError(f"phone_number {phone_number_id} not found")
        old = dict(old_row)
        await _lock_live_telephony_config(conn, fields.get("telephony_config_id"))

        columns = list(fields.keys())
        set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
        try:
            new_row = await conn.fetchrow(
                f"UPDATE phone_numbers SET {set_clause}, updated_at = now() "
                f"WHERE id = $1 RETURNING *",
                phone_number_id, *(fields[col] for col in columns),
            )
        except asyncpg.UniqueViolationError as exc:
            _raise_if_did_taken(exc, fields.get("did", old["did"]))
            raise
        new = _row(new_row)

        await audit.write_audit(
            conn,
            entity_type="phone_number",
            entity_id=phone_number_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
            new_value=new,
        )

    # Invalidate before get_by_did(), or its cache-aside read returns the stale route.
    if old["did"] != new["did"]:
        await cache.invalidate(_cache_key(old["did"]))
    await cache.invalidate(_cache_key(new["did"]))
    await get_by_did(new["did"])
    return new


async def soft_delete_phone_number(
    phone_number_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM phone_numbers WHERE id = $1 FOR UPDATE", phone_number_id,
        )
        if old_row is None:
            raise LookupError(f"phone_number {phone_number_id} not found")
        old = dict(old_row)

        await conn.execute(
            "UPDATE phone_numbers SET deleted_at = now() WHERE id = $1", phone_number_id,
        )
        await audit.write_audit(
            conn,
            entity_type="phone_number",
            entity_id=phone_number_id,
            action="deleted",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
        )

    await cache.invalidate(_cache_key(old["did"]))
