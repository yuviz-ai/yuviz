"""tool_provider_configs CRUD (cold-path admin config). No Redis cache: the
Conversation Service's ToolPolicyResolver reads Postgres directly."""

from __future__ import annotations

import json as _json
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, db
from .provider_configs import resolve_api_key_input

_UPDATABLE_FIELDS = {"name", "engine", "api_key_ref", "extra"}


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Parse `extra`: no JSONB codec is registered, so asyncpg returns it as a string."""
    result = dict(row)
    extra = result.get("extra")
    if isinstance(extra, str):
        result["extra"] = _json.loads(extra)
    return result


async def get_tool_provider_config(
    tool_provider_config_id: Any, *, platform_scoped: bool = False,
) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="tool-provider-configs-by-id") if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tool_provider_configs WHERE id = $1 AND deleted_at IS NULL",
            tool_provider_config_id,
        )
    return _row_to_dict(row) if row is not None else None


async def list_tool_provider_configs(tenant_id: Any, *, tool_name: str | None = None) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    conditions = ["tenant_id = $1", "deleted_at IS NULL"]
    params: list[Any] = [tenant_id]
    if tool_name is not None:
        params.append(tool_name)
        conditions.append(f"tool_name = ${len(params)}")

    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            f"SELECT * FROM tool_provider_configs WHERE {' AND '.join(conditions)} ORDER BY name",
            *params,
        )
    return [_row_to_dict(row) for row in rows]


async def create_tool_provider_config(
    *,
    tenant_id: Any,
    name: str,
    tool_name: str,
    engine: str,
    api_key_ref: str | None = None,
    api_key: str | None = None,
    extra: dict[str, Any] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    api_key_ref = resolve_api_key_input(api_key, api_key_ref)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO tool_provider_configs "
            "(tenant_id, name, tool_name, engine, api_key_ref, extra) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING *",
            tenant_id, name, tool_name, engine, api_key_ref,
            _json.dumps(extra) if extra is not None else None,
        )
        result = _row_to_dict(row)
        await audit.write_audit(
            conn,
            entity_type="tool_provider_config",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value=result,
        )
    return result


async def update_tool_provider_config(
    tool_provider_config_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    # api_key is folded into api_key_ref; absent means untouched, empty means clear.
    had_api_key = "api_key" in fields
    typed_key = fields.pop("api_key", None)
    if had_api_key or "api_key_ref" in fields:
        fields["api_key_ref"] = resolve_api_key_input(typed_key, fields.get("api_key_ref"))

    if not fields:
        raise ValueError("update_tool_provider_config() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_tool_provider_config() got non-updatable field(s): {unknown}")

    if "extra" in fields and fields["extra"] is not None:
        fields = {**fields, "extra": _json.dumps(fields["extra"])}

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM tool_provider_configs WHERE id = $1 FOR UPDATE", tool_provider_config_id,
        )
        if old_row is None:
            raise LookupError(f"tool_provider_config {tool_provider_config_id} not found")
        old = _row_to_dict(old_row)

        columns = list(fields.keys())
        set_parts = []
        for i, col in enumerate(columns):
            cast = "::jsonb" if col == "extra" else ""
            set_parts.append(f"{col} = ${i + 2}{cast}")
        new_row = await conn.fetchrow(
            f"UPDATE tool_provider_configs SET {', '.join(set_parts)}, updated_at = now() "
            f"WHERE id = $1 RETURNING *",
            tool_provider_config_id, *(fields[col] for col in columns),
        )
        new = _row_to_dict(new_row)

        # Only written columns, so a redacted api_key_ref doesn't appear changed on every update.
        await audit.write_audit(
            conn,
            entity_type="tool_provider_config",
            entity_id=tool_provider_config_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value={col: old[col] for col in columns},
            new_value={col: new[col] for col in columns},
        )
    return new


async def soft_delete_tool_provider_config(
    tool_provider_config_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM tool_provider_configs WHERE id = $1 FOR UPDATE", tool_provider_config_id,
        )
        if old_row is None:
            raise LookupError(f"tool_provider_config {tool_provider_config_id} not found")
        old = dict(old_row)

        await conn.execute(
            "UPDATE tool_provider_configs SET deleted_at = now() WHERE id = $1", tool_provider_config_id,
        )
        await audit.write_audit(
            conn,
            entity_type="tool_provider_config",
            entity_id=tool_provider_config_id,
            action="deleted",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
        )
