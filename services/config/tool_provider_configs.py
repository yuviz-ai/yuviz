"""
tool_provider_configs CRUD — cold-path admin config only. Unlike
provider_configs.py, this deliberately has NO Redis cache: nothing on the
call path reads through Config Service for this table. The Conversation
Service's ToolPolicyResolver (services/conversation/tools/policy_resolver.py)
queries Postgres directly with its own short-lived in-process TTL cache, a
documented v1 simplification — see that file's docstring for the known
conflict with libs/config_sdk's unrelated ToolSpec/get_tools() stub.

Same audited-mutation pattern as provider_configs.py otherwise. Returning
api_key_ref to a caller is fine: it's a reference path (e.g.
'env:CAL_API_KEY'), never a resolved secret.
"""

from __future__ import annotations

import json as _json
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, db
from .provider_configs import resolve_api_key_input

_UPDATABLE_FIELDS = {"name", "engine", "api_key_ref", "extra"}


def _row_to_dict(row: Any) -> dict[str, Any]:
    """asyncpg returns a JSONB column as a raw JSON string, not a parsed
    object, with no codec registered on this pool — every caller (the
    Admin UI included) expects a real object back. Same gap already
    documented/fixed in libs/config_sdk's cache_aside.py; provider_configs.py
    has the identical bug, not fixed here (out of scope for this change)."""
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
    # api_key is a credential, not a column — popped before the unknown-
    # field check, encrypted, and folded into api_key_ref. Absent from
    # `fields` means untouched; present-but-empty is an explicit clear.
    # Same pattern as provider_configs.update_provider_config.
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

        # Scoped to the written columns, not the full row — otherwise
        # api_key_ref (redacted either way) rides along on every update
        # and the UI can't tell "redacted, unchanged" from "redacted,
        # changed."
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
