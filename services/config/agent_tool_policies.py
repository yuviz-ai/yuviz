"""
agent_tool_policies CRUD — which tools a given agent may actually use,
admin-authored via this cold-path CRUD but resolved at call time by the
Conversation Service's own ToolPolicyResolver (direct Postgres read, not
through this service — see tool_provider_configs.py's docstring). No cache
here for the same reason.

Keyed by (agent_id, tool_name) rather than a bare policy id — the table's
own UNIQUE(agent_id, tool_name) constraint already makes that the natural
key, and it matches the Knowledge Platform's own
/agents/{agent_id}/knowledge-bases/{kb_id} attach/detach shape (see
services/knowledge) rather than inventing an opaque id the Admin UI would
have to track per row.

list_for_agent() joins tool_provider_configs so the Admin UI can render a
policy row (tool name, provider engine, enabled) without a second round
trip per row.
"""

from __future__ import annotations

from typing import Any

from . import audit, db

_UPDATABLE_FIELDS = {"enabled", "timeout_ms", "max_calls_per_turn", "max_chain_depth"}


async def get_agent_tool_policy(agent_id: Any, tool_name: str) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM agent_tool_policies WHERE agent_id = $1 AND tool_name = $2", agent_id, tool_name,
    )
    return dict(row) if row is not None else None


async def list_for_agent(agent_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        SELECT atp.*, tpc.name AS tool_provider_config_name, tpc.engine AS tool_provider_config_engine
        FROM agent_tool_policies atp
        JOIN tool_provider_configs tpc ON tpc.id = atp.tool_provider_config_id
        WHERE atp.agent_id = $1 AND tpc.deleted_at IS NULL
        ORDER BY atp.created_at
        """,
        agent_id,
    )
    return [dict(row) for row in rows]


async def create_agent_tool_policy(
    *,
    agent_id: Any,
    tool_name: str,
    tool_provider_config_id: Any,
    enabled: bool = True,
    timeout_ms: int | None = None,
    max_calls_per_turn: int | None = None,
    max_chain_depth: int | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO agent_tool_policies "
                "(agent_id, tool_name, tool_provider_config_id, enabled, timeout_ms, max_calls_per_turn, "
                " max_chain_depth) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                agent_id, tool_name, tool_provider_config_id, enabled, timeout_ms, max_calls_per_turn,
                max_chain_depth,
            )
            result = dict(row)
            await audit.write_audit(
                conn,
                entity_type="agent_tool_policy",
                entity_id=result["id"],
                action="created",
                user_id=user_id,
                user_email=user_email,
                new_value=result,
            )
    return result


async def update_agent_tool_policy(
    agent_id: Any,
    tool_name: str,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_agent_tool_policy() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_agent_tool_policy() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM agent_tool_policies WHERE agent_id = $1 AND tool_name = $2 FOR UPDATE",
                agent_id, tool_name,
            )
            if old_row is None:
                raise LookupError(f"agent_tool_policy for agent={agent_id} tool_name={tool_name!r} not found")
            old = dict(old_row)
            policy_id = old["id"]

            columns = list(fields.keys())
            set_parts = [f"{col} = ${i + 2}" for i, col in enumerate(columns)]
            new_row = await conn.fetchrow(
                f"UPDATE agent_tool_policies SET {', '.join(set_parts)}, updated_at = now() "
                f"WHERE id = $1 RETURNING *",
                policy_id, *(fields[col] for col in columns),
            )
            new = dict(new_row)

            await audit.write_audit(
                conn,
                entity_type="agent_tool_policy",
                entity_id=policy_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
                new_value=new,
            )
    return new


async def delete_agent_tool_policy(
    agent_id: Any, tool_name: str, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM agent_tool_policies WHERE agent_id = $1 AND tool_name = $2 FOR UPDATE",
                agent_id, tool_name,
            )
            if old_row is None:
                raise LookupError(f"agent_tool_policy for agent={agent_id} tool_name={tool_name!r} not found")
            old = dict(old_row)

            await conn.execute("DELETE FROM agent_tool_policies WHERE id = $1", old["id"])
            await audit.write_audit(
                conn,
                entity_type="agent_tool_policy",
                entity_id=old["id"],
                action="deleted",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
            )
