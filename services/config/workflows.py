"""
Workflow draft/publish/versions for agents.workflow (docs/workflow.md §4.2).

- workflow_draft: editor autosave; may be invalid; never read by a call
- workflow: live graph; only written by publish/create after validation
- agent_workflow_versions: append-only publish history (rollback republishes)

`workflow` is intentionally absent from agents._UPDATABLE_FIELDS so PATCH
cannot put an unvalidated graph on a live agent.
"""

from __future__ import annotations

import json
from typing import Any

from libs.config_sdk.workflow import (
    WorkflowError,
    WorkflowInvalid,
    graph_warnings,
    parse_graph,
)

from . import agents as agents_service
from . import audit, cache, db


class WorkflowValidationError(Exception):
    """Structured per-node/per-edge errors for the editor (docs/workflow.md §5.1)."""

    def __init__(self, errors: list[WorkflowError]) -> None:
        self.errors = errors
        super().__init__("workflow is not valid")


def validate(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Raise WorkflowValidationError on errors; return warnings (never blocking)."""
    try:
        parsed = parse_graph(graph)
    except WorkflowInvalid as exc:
        raise WorkflowValidationError(exc.errors) from None
    return [w.to_dict() for w in graph_warnings(parsed)]


async def _locked_agent(conn: Any, agent_id: Any, tenant_slug: str) -> dict[str, Any]:
    """Tenant-scoped SELECT ... FOR UPDATE (same shape as agents.update_agent)."""
    row = await conn.fetchrow(
        "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE a.id = $1 AND t.slug = $2 AND a.deleted_at IS NULL FOR UPDATE OF a",
        agent_id, tenant_slug,
    )
    if row is None:
        raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
    return dict(row)


def _as_graph(value: Any) -> dict[str, Any] | None:
    return db.json_col(value)


async def append_version(
    conn: Any,
    agent_id: Any,
    graph: dict[str, Any] | str,
    *,
    user_id: Any | None = None,
    note: str | None = None,
) -> int:
    """Insert the next agent_workflow_versions row; returns the new version number."""
    version = await conn.fetchval(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_workflow_versions WHERE agent_id = $1",
        agent_id,
    )
    payload = graph if isinstance(graph, str) else json.dumps(graph)
    await conn.execute(
        "INSERT INTO agent_workflow_versions (agent_id, version, graph, published_by, note) "
        "VALUES ($1, $2, $3::jsonb, $4, $5)",
        agent_id, version, payload, user_id, note,
    )
    return version


async def get_workflow(agent_id: Any, tenant_slug: str) -> dict[str, Any]:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT a.workflow, a.workflow_draft FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE a.id = $1 AND t.slug = $2 AND a.deleted_at IS NULL",
        agent_id, tenant_slug,
    )
    if row is None:
        raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
    published = _as_graph(row["workflow"])
    return {
        "workflow": published,
        "workflow_draft": _as_graph(row["workflow_draft"]),
        "published": published is not None,
    }


async def save_draft(
    agent_id: Any, *, tenant_slug: str, graph: dict[str, Any],
) -> dict[str, Any]:
    """Autosave. Last-write-wins; tenant + soft-delete enforced on the UPDATE itself."""
    pool = await db.get_pool()
    status = await pool.execute(
        """
        UPDATE agents a
           SET workflow_draft = $3::jsonb
          FROM tenants t
         WHERE a.id = $1
           AND t.id = a.tenant_id
           AND t.slug = $2
           AND a.deleted_at IS NULL
        """,
        agent_id, tenant_slug, json.dumps(graph),
    )
    if status == "UPDATE 0":
        raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
    return {"saved": True}


async def publish(
    agent_id: Any,
    *,
    tenant_slug: str,
    graph: dict[str, Any] | None = None,
    note: str | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Validate, write live graph + draft, append a version, bump config_version.

    graph=None publishes workflow_draft (editor Publish button).
    Identical to the already-live graph is a no-op (no version row, no bump).
    """
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await _locked_agent(conn, agent_id, tenant_slug)
            candidate = graph if graph is not None else _as_graph(old["workflow_draft"])
            if candidate is None:
                raise ValueError("nothing to publish — this agent has no workflow draft")
            warnings = validate(candidate)

            current = _as_graph(old["workflow"])
            if candidate == current:
                version = await conn.fetchval(
                    "SELECT COALESCE(MAX(version), 0) FROM agent_workflow_versions WHERE agent_id = $1",
                    agent_id,
                )
                return {
                    "version": version,
                    "config_version": old["config_version"],
                    "warnings": warnings,
                }

            payload = json.dumps(candidate)
            new_row = await conn.fetchrow(
                "UPDATE agents SET workflow = $2::jsonb, workflow_draft = $2::jsonb "
                "WHERE id = $1 RETURNING *",
                agent_id, payload,
            )
            version = await append_version(
                conn, agent_id, payload, user_id=user_id, note=note,
            )
            await audit.write_audit(
                conn,
                entity_type="agent_workflow",
                entity_id=agent_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value={"workflow": current},
                new_value={"workflow": candidate, "version": version},
            )
            new = dict(new_row)

    await cache.invalidate(agents_service._cache_key(tenant_slug, new["slug"]))
    return {
        "version": version,
        "config_version": new["config_version"],
        "warnings": warnings,
    }


async def list_versions(agent_id: Any, tenant_slug: str, limit: int = 50) -> list[dict[str, Any]]:
    """Version summaries only; use get_version() for a full graph."""
    limit = max(1, min(limit, 200))
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        SELECT v.id, v.version, v.published_at, v.note, u.email AS published_by_email,
               jsonb_array_length(COALESCE(v.graph -> 'nodes', '[]'::jsonb)) AS node_count,
               jsonb_array_length(COALESCE(v.graph -> 'edges', '[]'::jsonb)) AS edge_count
        FROM agent_workflow_versions v
        JOIN agents a ON a.id = v.agent_id
        JOIN tenants t ON t.id = a.tenant_id
        LEFT JOIN users u ON u.id = v.published_by
        WHERE v.agent_id = $1 AND t.slug = $2 AND a.deleted_at IS NULL
        ORDER BY v.version DESC
        LIMIT $3
        """,
        agent_id, tenant_slug, limit,
    )
    return [dict(row) for row in rows]


async def get_version(agent_id: Any, tenant_slug: str, version: int) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT v.* FROM agent_workflow_versions v "
        "JOIN agents a ON a.id = v.agent_id JOIN tenants t ON t.id = a.tenant_id "
        "WHERE v.agent_id = $1 AND t.slug = $2 AND v.version = $3 AND a.deleted_at IS NULL",
        agent_id, tenant_slug, version,
    )
    if row is None:
        return None
    result = dict(row)
    result["graph"] = _as_graph(result["graph"])
    return result


async def rollback(
    agent_id: Any, *, tenant_slug: str, version: int,
    user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """Republish an old version as a new append-only entry."""
    old_version = await get_version(agent_id, tenant_slug, version)
    if old_version is None:
        raise LookupError(f"workflow version {version} not found for agent {agent_id}")
    return await publish(
        agent_id, tenant_slug=tenant_slug, graph=old_version["graph"],
        note=f"rollback to version {version}", user_id=user_id, user_email=user_email,
    )
