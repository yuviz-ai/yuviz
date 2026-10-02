"""Tenant CRUD: cache-aside reads (Redis, 60s TTL), mutations audited in the same transaction."""

from __future__ import annotations

import uuid
from typing import Any

from libs.tenancy import platform_conn

from . import audit, cache, db, phone_numbers
from .provider_configs import require_usable_tts_voice

# Allow-list, so a typo'd field fails loudly instead of building a bad column reference.
_UPDATABLE_FIELDS = {
    "name", "region",
    "vad_engine", "vad_onset_ms", "vad_hold_ms", "vad_speech_threshold",
    "no_speech_timeout_ms", "stt_timeout_ms", "llm_timeout_ms",
    "transfer_timeout_ms",
    "default_stt_config_id", "default_llm_config_id", "default_tts_config_id",
    "max_concurrent_calls",
}


async def _validate_default_providers(conn: Any, tenant_id: Any, fields: dict[str, Any]) -> None:
    """An account default must be this account's own live provider of that
    role; agents without their own provider run on it."""
    for role in ("stt", "llm", "tts"):
        field = f"default_{role}_config_id"
        config_id = fields.get(field)
        if config_id is None:
            continue
        try:
            uuid.UUID(str(config_id))
        except ValueError:
            raise ValueError(f"{field}={config_id!r} is not a valid id") from None
        # FOR SHARE: serializes with a concurrent delete's FOR UPDATE.
        row = await conn.fetchrow(
            "SELECT tenant_id, role, engine, voice FROM provider_configs WHERE id = $1 AND deleted_at IS NULL FOR SHARE",
            config_id,
        )
        if row is None or str(row["tenant_id"]) != str(tenant_id):
            raise ValueError(f"{field}={config_id!r} is not one of this account's providers")
        if row["role"] != role:
            raise ValueError(f"{field}={config_id!r} is a {row['role']} provider, not {role}")
        require_usable_tts_voice(field, config_id, row["engine"], row["voice"])


def _cache_key(slug: str) -> str:
    return f"tenant:{slug}"


async def get_tenant(slug: str) -> dict[str, Any] | None:
    cached = await cache.get_json(_cache_key(slug))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    async with platform_conn(pool, reason="tenants-out-of-rls-scope") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tenants WHERE slug = $1 AND deleted_at IS NULL", slug,
        )
    if row is None:
        return None

    result = dict(row)
    await cache.set_json(_cache_key(slug), result)
    return result


async def get_tenant_by_id(tenant_id: Any) -> dict[str, Any] | None:
    """Uncached; used for cold-path existence checks."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="tenants-out-of-rls-scope") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tenants WHERE id = $1 AND deleted_at IS NULL", tenant_id,
        )
    return dict(row) if row is not None else None


async def list_tenants(*, tenant_id: Any | None = None) -> list[dict[str, Any]]:
    """`tenant_id=None` (platform-scoped actors only) returns every live tenant; otherwise just that one."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="tenants-out-of-rls-scope") as conn:
        if tenant_id is None:
            rows = await conn.fetch(
                "SELECT * FROM tenants WHERE deleted_at IS NULL ORDER BY name",
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM tenants WHERE id = $1 AND deleted_at IS NULL ORDER BY name", tenant_id,
            )
    return [dict(row) for row in rows]


async def create_tenant(
    *,
    name: str,
    slug: str,
    region: str = "us",
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="tenants-out-of-rls-scope") as conn:
        row = await conn.fetchrow(
            "INSERT INTO tenants (name, slug, region) VALUES ($1, $2, $3) RETURNING *",
            name, slug, region,
        )
        result = dict(row)
        await audit.write_audit(
            conn,
            entity_type="tenant",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value=result,
        )
    return result


async def update_tenant(
    tenant_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_tenant() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_tenant() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with platform_conn(pool, reason="tenants-out-of-rls-scope") as conn:
        # Before the tenants lock: provider delete locks the provider, then
        # the tenant, so this must take them in the same order.
        await _validate_default_providers(conn, tenant_id, fields)
        # FOR UPDATE so a concurrent update can't make the audit old_value stale.
        old_row = await conn.fetchrow(
            "SELECT * FROM tenants WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", tenant_id,
        )
        if old_row is None:
            raise LookupError(f"tenant {tenant_id} not found")
        old = dict(old_row)

        columns = list(fields.keys())
        set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
        new_row = await conn.fetchrow(
            f"UPDATE tenants SET {set_clause} WHERE id = $1 RETURNING *",
            tenant_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

        await audit.write_audit(
            conn,
            entity_type="tenant",
            entity_id=tenant_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
            new_value=new,
        )

    await cache.invalidate(_cache_key(old["slug"]))
    return new


class TenantHasActiveResources(Exception):
    """Raised instead of deleting a tenant with active agents/numbers unless force=True;
    deletion stops its DIDs routing immediately."""

    def __init__(self, active_agents: int, active_phone_numbers: int) -> None:
        self.active_agents = active_agents
        self.active_phone_numbers = active_phone_numbers
        super().__init__(
            f"tenant has {active_agents} active agent(s) and {active_phone_numbers} "
            "active phone number(s) — pass force=True to delete anyway"
        )


async def soft_delete_tenant(
    tenant_id: Any, *, user_id: Any | None = None, user_email: str | None = None, force: bool = False,
) -> None:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="tenants-out-of-rls-scope") as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM tenants WHERE id = $1 FOR UPDATE", tenant_id,
        )
        if old_row is None:
            raise LookupError(f"tenant {tenant_id} not found")
        old = dict(old_row)

        if not force:
            active_agents = await conn.fetchval(
                "SELECT count(*) FROM agents WHERE tenant_id = $1 AND deleted_at IS NULL AND status = 'active'",
                tenant_id,
            )
            active_phone_numbers = await conn.fetchval(
                "SELECT count(*) FROM phone_numbers WHERE tenant_id = $1 AND deleted_at IS NULL AND status = 'active'",
                tenant_id,
            )
            if active_agents or active_phone_numbers:
                raise TenantHasActiveResources(active_agents, active_phone_numbers)

        await conn.execute(
            "UPDATE tenants SET deleted_at = now() WHERE id = $1", tenant_id,
        )
        await audit.write_audit(
            conn,
            entity_type="tenant",
            entity_id=tenant_id,
            action="deleted",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
        )
        # A deleted tenant's numbers must stop routing; did:{did} has no TTL,
        # so nothing else would ever remove them.
        retired = await conn.fetch(
            "UPDATE phone_numbers SET deleted_at = now() WHERE tenant_id = $1 AND deleted_at IS NULL "
            "RETURNING *",
            tenant_id,
        )
        for row in retired:
            await audit.write_audit(
                conn,
                entity_type="phone_number",
                entity_id=row["id"],
                action="deleted",
                user_id=user_id,
                user_email=user_email,
                old_value=dict(row),
            )

    await cache.invalidate(_cache_key(old["slug"]))
    for row in retired:
        await cache.invalidate(phone_numbers._cache_key(row["did"]))
