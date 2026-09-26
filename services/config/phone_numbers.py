"""
Phone number (DID) CRUD — same cache-aside + audited-mutation pattern as
tenants.py/agents.py/provider_configs.py.

Two distinct read shapes, deliberately not unified:
  - get_by_did() returns a DENORMALIZED {"tenant_slug", "agent_slug", "version"}
    dict, cached under "did:{did}" — this is the exact JSON shape the
    Gateway's C++ RedisClient reads directly on every inbound call (see
    gateway/src/config/Config.cpp's PhoneRoute::from_redis()), so the hot
    path never needs a second Postgres join at call time. "version" is the
    resolved agent's config_version (null if agent_slug fell through to the
    literal 'default' with no real agent row) — lets a consumer detect a
    stale-vs-current resolved agent config without a second lookup.
  - Everything else (get/list/create/update/soft_delete) operates on the raw
    phone_numbers row (ids, not slugs) — this is what the Admin UI's
    CRUD forms actually edit.
"""

from __future__ import annotations

import logging
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, db

log = logging.getLogger(__name__)

_UPDATABLE_FIELDS = {"did", "agent_id", "fallback_agent_id", "carrier_id", "telephony_config_id", "region", "status"}

# DID -> tenant/agent routing has NO TTL at all — deliberately, per canonical
# design (project memory 2026-07-14). Two TTL-based designs were tried and
# rejected here before landing on this one:
#   - cache.DEFAULT_TTL_SECONDS (60s): far too short, expires mid-session.
#   - A custom longer TTL (tried: 600s, then 86400s): still wrong in kind,
#     not just degree. The Gateway's PhoneRoute::from_redis() NEVER falls
#     through to Postgres on a miss (hot-path rule: Redis-only) — so ANY
#     expiry, no matter how generous, is a live landmine: the entry goes
#     cold, and every subsequent real call silently and PERMANENTLY
#     misroutes to tenant=default/agent=default until something unrelated
#     happens to re-read that exact DID. This bit for real, twice, at two
#     different TTL values (see project memory 2026-07-13/14).
#
# The actual fix is to stop expiring this data at all: DID->tenant/agent
# assignment changes at provisioning time, not mid-session, so Redis can
# just hold the current value forever, updated in lockstep with Postgres:
#   - create_phone_number() writes the fresh value straight into Redis.
#   - update_phone_number() writes the fresh value straight into Redis
#     (not just invalidate-and-hope-something-reads-it-soon).
#   - soft_delete_phone_number() deletes the key (the DID no longer routes
#     anywhere, so there's nothing to keep cached).
#   - prewarm() (services/config/app.py's lifespan) preloads every active
#     DID once at startup, covering a fresh/flushed Redis.
# Every code path that can change a DID's route already runs synchronously
# against Postgres in the same request, so "keep Redis and Postgres in sync
# on every write" is exactly as strong a guarantee as a TTL ever was, minus
# the window where a stale/absent entry silently misroutes a real call.


def _cache_key(did: str) -> str:
    return f"did:{did}"


async def get_by_did(did: str, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """`platform_scoped=True` only for prewarm()'s startup sweep, which has
    no request tenant to scope to; every in-request caller (post-write
    warm below) takes the ambient tenant_conn() ceiling instead."""
    cached = await cache.get_json(_cache_key(did))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="phone-numbers-did-lookup")
        if platform_scoped
        else tenant_conn(pool)
    )
    # A non-'active' phone_numbers.status routes exactly like an unrecognized
    # DID (falls through to the caller's default) — a suspended/inactive
    # number must not keep resolving to its normal agent just because the
    # row still exists. agent_slug prefers the primary agent, falls back to
    # fallback_agent_id if the primary is unset, deleted, OR itself
    # deactivated (agents.status = 'inactive' — a reversible pause, distinct
    # from deleted_at), and finally to "default" if neither resolves — same
    # three-tier fallback PhoneRoute::from_redis() already applies for a
    # total miss.
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
        # Agent's config_version — lets a consumer detect "the resolved
        # agent's config has changed since this route was cached" (e.g. for
        # logging/observability). null when agent_slug fell all the way
        # through to the literal 'default' with no real agent row backing
        # it (a phone_numbers row with no agent_id/fallback_agent_id set).
        "version": row["agent_config_version"],
    }
    await cache.set_json(_cache_key(did), result, ttl=None)
    return result


async def prewarm() -> int:
    """Populate did:{did} for every active phone number. Called once at
    Config Service startup (see app.py's lifespan) — covers a fresh/flushed
    Redis, the one gap not already handled by write-through (see top-of-file
    comment). Returns the number of DIDs warmed, for startup logging.
    """
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
    return dict(row) if row is not None else None


async def list_phone_numbers(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM phone_numbers WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY did",
            tenant_id,
        )
    return [dict(row) for row in rows]


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
        row = await conn.fetchrow(
            "INSERT INTO phone_numbers "
            "(tenant_id, did, agent_id, fallback_agent_id, carrier_id, telephony_config_id, region, status) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
            tenant_id, did, agent_id, fallback_agent_id, carrier_id, telephony_config_id, region, status,
        )
        result = dict(row)
        await audit.write_audit(
            conn,
            entity_type="phone_number",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value=result,
        )
    # Warm the cache immediately rather than lazily on first call — see this
    # module's top-of-file comment for why.
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
        # FOR UPDATE — see tenants.py's update_tenant() comment: without
        # it, a concurrent update could make this transaction's audit
        # entry record a stale old_value.
        old_row = await conn.fetchrow(
            "SELECT * FROM phone_numbers WHERE id = $1 FOR UPDATE", phone_number_id,
        )
        if old_row is None:
            raise LookupError(f"phone_number {phone_number_id} not found")
        old = dict(old_row)

        columns = list(fields.keys())
        set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
        new_row = await conn.fetchrow(
            f"UPDATE phone_numbers SET {set_clause}, updated_at = now() "
            f"WHERE id = $1 RETURNING *",
            phone_number_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

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

    # Write-through (see top-of-file comment). Must invalidate new["did"]'s
    # key BEFORE calling get_by_did(): if the did string itself didn't
    # change, the old cached value sits under that exact same key, and
    # get_by_did()'s cache-aside check would just return the stale hit
    # instead of re-reading Postgres. If the DID itself changed, the *old*
    # DID string no longer routes anywhere, so its key is deleted outright.
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
