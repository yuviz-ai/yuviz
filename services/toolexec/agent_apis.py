"""Per-agent custom API enablement (agent_custom_apis allow-list; no row = disabled) and its auth gate."""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from services.config.auth import CurrentUser
from services.config.deps import is_platform_scoped

from . import audit, db, graph

# Fixed string for every rejection cause, so ids aren't a cross-tenant existence oracle.
_NOT_FOUND_DETAIL = "agent or custom API not found"


async def _authorize_agent_api(
    agent_id: Any, custom_api_id: Any | None, current_user: CurrentUser,
) -> tuple[dict, dict | None]:
    """Authorize agent (and custom API, joined on the same tenant) before any write.
    Raises LookupError with an identical detail for missing, soft-deleted or foreign rows."""
    pool = await db.get_pool()
    platform_scoped = is_platform_scoped(current_user)
    conn_cm = (
        platform_conn(pool, reason="agent-apis-admin-by-id") if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT a.id AS agent_id, a.tenant_id AS tenant_id, "
            "       ca.id AS custom_api_id, ca.chain_levels AS chain_levels "
            "FROM agents a "
            "LEFT JOIN custom_apis ca "
            "       ON ca.id = $2 AND ca.tenant_id = a.tenant_id AND ca.deleted_at IS NULL "
            "WHERE a.id = $1 AND a.deleted_at IS NULL",
            agent_id, custom_api_id,
        )
    if row is None:
        raise LookupError(_NOT_FOUND_DETAIL)
    if custom_api_id is not None and row["custom_api_id"] is None:
        raise LookupError(_NOT_FOUND_DETAIL)
    if str(row["tenant_id"]) != current_user.tenant_id and not is_platform_scoped(current_user):
        raise LookupError(_NOT_FOUND_DETAIL)

    agent = {"id": row["agent_id"], "tenant_id": row["tenant_id"]}
    custom_api = (
        {"id": row["custom_api_id"], "chain_levels": row["chain_levels"]}
        if row["custom_api_id"] is not None else None
    )
    return agent, custom_api


async def _effective_max_chain_depth(agent_id: Any, *, platform_scoped: bool = False) -> int:
    """Agent's max_chain_depth, clamped to graph.MAX_CHAIN_LEVELS (NULL = platform ceiling).
    platform_scoped must come from is_platform_scoped(current_user)."""
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="agent-apis-admin-by-id") if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT max_chain_depth FROM agent_tool_policies WHERE agent_id = $1 AND tool_name = 'execute_api'",
            agent_id,
        )
    if row is None or row["max_chain_depth"] is None:
        return graph.MAX_CHAIN_LEVELS
    return min(row["max_chain_depth"], graph.MAX_CHAIN_LEVELS)


async def list_for_agent(agent_id: Any, *, current_user: CurrentUser) -> list[dict]:
    platform_scoped = is_platform_scoped(current_user)
    await _authorize_agent_api(agent_id, None, current_user)
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="agent-apis-admin-by-id") if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        rows = await conn.fetch(
            "SELECT aca.*, ca.name, ca.chain_levels FROM agent_custom_apis aca "
            "JOIN custom_apis ca ON ca.id = aca.custom_api_id AND ca.deleted_at IS NULL "
            "WHERE aca.agent_id = $1 ORDER BY ca.name",
            agent_id,
        )
    return [dict(row) for row in rows]


async def set_enabled(
    agent_id: Any,
    custom_api_id: Any,
    *,
    enabled: bool,
    current_user: CurrentUser,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict:
    """Enable/disable one (agent, custom_api) pair; authorization runs before any write."""
    platform_scoped = is_platform_scoped(current_user)
    _agent, custom_api = await _authorize_agent_api(agent_id, custom_api_id, current_user)
    assert custom_api is not None  # guaranteed once _authorize_agent_api returns for a given id

    if enabled:
        effective_ceiling = await _effective_max_chain_depth(agent_id, platform_scoped=platform_scoped)
        if custom_api["chain_levels"] > effective_ceiling:
            raise ValueError(
                f"chain_depth_exceeds_agent_ceiling: api chain_levels={custom_api['chain_levels']} "
                f"> effective max_chain_depth={effective_ceiling}"
            )

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="agent-apis-admin-mutation", stamp_tenant=_agent["tenant_id"])
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO agent_custom_apis (agent_id, custom_api_id, enabled) VALUES ($1, $2, $3) "
                "ON CONFLICT (agent_id, custom_api_id) DO UPDATE SET enabled = $3, updated_at = now() "
                "RETURNING *",
                agent_id, custom_api_id, enabled,
            )
            result = dict(row)
            await audit.write_audit(
                conn, entity_type="custom_api", entity_id=custom_api_id, action="updated",
                user_id=user_id, user_email=user_email, new_value=result,
            )
    return result


async def detach(
    agent_id: Any,
    custom_api_id: Any,
    *,
    current_user: CurrentUser,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> None:
    platform_scoped = is_platform_scoped(current_user)
    agent, _custom_api = await _authorize_agent_api(agent_id, custom_api_id, current_user)
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="agent-apis-admin-mutation", stamp_tenant=agent["tenant_id"])
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM agent_custom_apis WHERE agent_id = $1 AND custom_api_id = $2",
                agent_id, custom_api_id,
            )
            await audit.write_audit(
                conn, entity_type="custom_api", entity_id=custom_api_id, action="deleted",
                user_id=user_id, user_email=user_email,
            )


# Same detail for missing and foreign sessions (no existence oracle).
_CHAIN_RUNS_NOT_FOUND_DETAIL = "no chain runs found for this session"


async def _authorize_chain_runs(session_id: Any, current_user: CurrentUser) -> list[dict]:
    """session_id carries no tenant, so the tenant predicate must be in the query itself."""
    pool = await db.get_pool()
    tenant_filter = None if is_platform_scoped(current_user) else current_user.tenant_id
    conn_cm = (
        platform_conn(pool, reason="agent-apis-chain-runs") if tenant_filter is None else tenant_conn(pool)
    )
    async with conn_cm as conn:
        run_rows = await conn.fetch(
            "SELECT * FROM api_chain_runs WHERE session_id = $1 AND ($2::uuid IS NULL OR tenant_id = $2) "
            "ORDER BY started_at",
            session_id, tenant_filter,
        )
        if not run_rows:
            raise LookupError(_CHAIN_RUNS_NOT_FOUND_DETAIL)

        runs = []
        for run_row in run_rows:
            run = dict(run_row)
            step_rows = await conn.fetch(
                "SELECT * FROM api_chain_steps WHERE run_id = $1 ORDER BY step_index", run["id"],
            )
            run["steps"] = [dict(s) for s in step_rows]
            runs.append(run)
    return runs
