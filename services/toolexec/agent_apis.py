"""
services/toolexec/agent_apis.py — per-agent custom API enablement (the
agent_custom_apis allow-list — no row means not enabled, not a broken
default, same posture as agent_knowledge_bases / agent_tool_policies) and
the AC 10 write-time authorization gate protecting every route that
touches it.

Imports services.config.auth/deps directly for CurrentUser and
is_platform_scoped, the same explicit choice services/toolexec/app.py's
docstring already commits to (T2) — not the audit.py-style duplicated
copy, because this is an authorization PREDICATE this service has no
business re-implementing, not a query or model it would otherwise avoid
depending on another service for.
"""

from __future__ import annotations

from typing import Any

from services.config.auth import CurrentUser
from services.config.deps import is_platform_scoped

from . import audit, db, graph

# Deliberately a single fixed string, never interpolated with the agent_id
# or custom_api_id that was rejected — a missing agent, a missing/soft-
# deleted/wrong-tenant custom API, and a caller from the wrong tenant must
# all be byte-identical (lesson 2), so a tenant-A admin who guesses tenant
# B's custom_api_id learns nothing a random UUID would not also tell them.
_NOT_FOUND_DETAIL = "agent or custom API not found"


async def _authorize_agent_api(
    agent_id: Any, custom_api_id: Any | None, current_user: CurrentUser,
) -> tuple[dict, dict | None]:
    """One query joining both sides to the same tenant:

        SELECT a.id AS agent_id, a.tenant_id, ca.id AS custom_api_id, ca.chain_levels
        FROM agents a
        LEFT JOIN custom_apis ca
               ON ca.id = $2 AND ca.tenant_id = a.tenant_id AND ca.deleted_at IS NULL
        WHERE a.id = $1 AND a.deleted_at IS NULL

    Raises LookupError (-> 404, identical detail text in every case) when
    the agent does not exist or is soft-deleted, when custom_api_id was
    given but did not join (absent, soft-deleted, or a DIFFERENT tenant's
    API — AC 10), or when the agent's tenant is not the caller's and the
    caller is not platform-scoped (`is_platform_scoped`, lesson 24).
    custom_api_id=None is the list route, which checks only the agent
    side. Runs BEFORE any INSERT/UPDATE/DELETE — every caller below calls
    this first."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
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


async def _effective_max_chain_depth(agent_id: Any) -> int:
    """NULL agent_tool_policies.max_chain_depth = use the platform ceiling
    (graph.MAX_CHAIN_LEVELS); a set value can only LOWER the ceiling, never
    raise it — the same one-directional clamp chain_budget_ms gets at
    request time, so no per-agent override can exceed the platform-wide
    depth backstop."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT max_chain_depth FROM agent_tool_policies WHERE agent_id = $1 AND tool_name = 'execute_api'",
        agent_id,
    )
    if row is None or row["max_chain_depth"] is None:
        return graph.MAX_CHAIN_LEVELS
    return min(row["max_chain_depth"], graph.MAX_CHAIN_LEVELS)


async def list_for_agent(agent_id: Any, *, current_user: CurrentUser) -> list[dict]:
    await _authorize_agent_api(agent_id, None, current_user)
    pool = await db.get_pool()
    rows = await pool.fetch(
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
    """Enable/disable one (agent, custom_api) pair. Authorization (and thus
    the 404 for a cross-tenant custom_api_id) happens before this ever
    reaches the INSERT/UPDATE — _authorize_agent_api raises first, so a
    rejected attempt leaves agent_custom_apis's row count unchanged."""
    _agent, custom_api = await _authorize_agent_api(agent_id, custom_api_id, current_user)
    assert custom_api is not None  # guaranteed once _authorize_agent_api returns for a given id

    if enabled:
        effective_ceiling = await _effective_max_chain_depth(agent_id)
        if custom_api["chain_levels"] > effective_ceiling:
            raise ValueError(
                f"chain_depth_exceeds_agent_ceiling: api chain_levels={custom_api['chain_levels']} "
                f"> effective max_chain_depth={effective_ceiling}"
            )

    pool = await db.get_pool()
    async with pool.acquire() as conn:
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
    await _authorize_agent_api(agent_id, custom_api_id, current_user)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM agent_custom_apis WHERE agent_id = $1 AND custom_api_id = $2",
                agent_id, custom_api_id,
            )
            await audit.write_audit(
                conn, entity_type="custom_api", entity_id=custom_api_id, action="deleted",
                user_id=user_id, user_email=user_email,
            )


# Same fixed detail for "no such session" and "exists, but every run in it
# belongs to a different tenant" — a foreign session_id must not be an
# existence oracle either (lesson 2).
_CHAIN_RUNS_NOT_FOUND_DETAIL = "no chain runs found for this session"


async def _authorize_chain_runs(session_id: Any, current_user: CurrentUser) -> list[dict]:
    """api_chain_runs.session_id is unscoped opaque TEXT — it carries no
    tenant of its own — so the tenant predicate has to be IN THE QUERY,
    not a filter applied to the result afterward (finding 2). A
    tenant-scoped caller sees only their own tenant's runs; a
    platform-scoped caller (is_platform_scoped, lesson 24) sees the
    session unfiltered. Indexed by idx_api_chain_runs_tenant_session."""
    pool = await db.get_pool()
    tenant_filter = None if is_platform_scoped(current_user) else current_user.tenant_id
    run_rows = await pool.fetch(
        "SELECT * FROM api_chain_runs WHERE session_id = $1 AND ($2::uuid IS NULL OR tenant_id = $2) "
        "ORDER BY started_at",
        session_id, tenant_filter,
    )
    if not run_rows:
        raise LookupError(_CHAIN_RUNS_NOT_FOUND_DETAIL)

    runs = []
    for run_row in run_rows:
        run = dict(run_row)
        step_rows = await pool.fetch(
            "SELECT * FROM api_chain_steps WHERE run_id = $1 ORDER BY step_index", run["id"],
        )
        run["steps"] = [dict(s) for s in step_rows]
        runs.append(run)
    return runs
