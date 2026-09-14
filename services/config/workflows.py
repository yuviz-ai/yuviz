"""
Workflow draft/publish/versions for agents.workflow (docs/workflow.md §4.2).

- workflow_draft: editor autosave; may be invalid; never read by a call
- workflow: live graph; only written by publish/create after validation
- agent_workflow_versions: append-only publish history (rollback republishes)

`workflow` is intentionally absent from agents._UPDATABLE_FIELDS so PATCH
cannot put an unvalidated graph on a live agent.

Until the Conversation FSM reads ConversationInfo.workflow, live calls still
use agents.greeting / agents.system_prompt. publish() mirrors those columns
from the start/global nodes so a Publish 200 means callers hear the new text.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from libs.config_sdk.workflow import (
    WorkflowError,
    WorkflowInvalid,
    graph_warnings,
    graphs_equivalent,
    parse_graph,
)

from . import agents as agents_service
from . import audit, cache, db


class WorkflowValidationError(Exception):
    """Structured per-node/per-edge errors for the editor (docs/workflow.md §5.1)."""

    def __init__(self, errors: list[WorkflowError]) -> None:
        self.errors = errors
        super().__init__("workflow is not valid")


class StaleDraft(Exception):
    """Draft PUT lost a race with a publish (config_version moved)."""


def _validate_sync(graph: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        parsed = parse_graph(graph)
    except WorkflowInvalid as exc:
        raise WorkflowValidationError(exc.errors) from None
    return [w.to_dict() for w in graph_warnings(parsed)]


async def validate(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """CPU-bound parse + warnings off the event loop (Config also serves call setup)."""
    return await asyncio.to_thread(_validate_sync, graph)


def prompts_from_graph(graph: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract start.greeting / global.prompt; None if that node type is absent."""
    greeting: str | None = None
    system_prompt: str | None = None
    saw_start = False
    saw_global = False
    for node in graph.get("nodes") or []:
        data = node.get("data") or {}
        if node.get("type") == "start":
            saw_start = True
            greeting = "" if data.get("greeting") is None else str(data.get("greeting"))
        elif node.get("type") == "global":
            saw_global = True
            system_prompt = "" if data.get("prompt") is None else str(data.get("prompt"))
    return (greeting if saw_start else None, system_prompt if saw_global else None)


def column_prompts(graph: dict[str, Any]) -> tuple[str, str]:
    """Columns the runtime reads today. Missing start/global → empty, not leftover."""
    greeting, system_prompt = prompts_from_graph(graph)
    return ("" if greeting is None else greeting, "" if system_prompt is None else system_prompt)


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
        "SELECT a.workflow, a.workflow_draft, a.config_version "
        "FROM agents a JOIN tenants t ON t.id = a.tenant_id "
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
        "config_version": row["config_version"],
    }


async def save_draft(
    agent_id: Any,
    *,
    tenant_slug: str,
    graph: dict[str, Any],
    base_config_version: int | None = None,
) -> dict[str, Any]:
    """Autosave. Optional base_config_version fences publish races (409 StaleDraft).

    Draft is not cached on the agent row (GET /agents strips it), so no cache
    invalidation — GET .../workflow always reads Postgres.
    """
    pool = await db.get_pool()
    if base_config_version is None:
        row = await pool.fetchrow(
            """
            UPDATE agents a
               SET workflow_draft = $3::jsonb
              FROM tenants t
             WHERE a.id = $1
               AND t.id = a.tenant_id
               AND t.slug = $2
               AND a.deleted_at IS NULL
         RETURNING a.config_version
            """,
            agent_id, tenant_slug, json.dumps(graph),
        )
    else:
        row = await pool.fetchrow(
            """
            UPDATE agents a
               SET workflow_draft = $3::jsonb
              FROM tenants t
             WHERE a.id = $1
               AND t.id = a.tenant_id
               AND t.slug = $2
               AND a.deleted_at IS NULL
               AND a.config_version = $4
         RETURNING a.config_version
            """,
            agent_id, tenant_slug, json.dumps(graph), base_config_version,
        )
    if row is None:
        exists = await pool.fetchrow(
            """
            SELECT a.config_version FROM agents a
              JOIN tenants t ON t.id = a.tenant_id
             WHERE a.id = $1 AND t.slug = $2 AND a.deleted_at IS NULL
            """,
            agent_id, tenant_slug,
        )
        if exists is None:
            raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
        if base_config_version is not None:
            raise StaleDraft()
        raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
    return {"saved": True, "config_version": row["config_version"]}


async def _peek_draft(agent_id: Any, tenant_slug: str) -> dict[str, Any] | None:
    """Unlocked draft read so validate() can run before FOR UPDATE."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT a.workflow_draft FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE a.id = $1 AND t.slug = $2 AND a.deleted_at IS NULL",
        agent_id, tenant_slug,
    )
    if row is None:
        raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
    return _as_graph(row["workflow_draft"])


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
    Identical logic to the already-live graph (ignoring RF chrome / order)
    syncs positions onto workflow + workflow_draft without a new version row;
    config_version still bumps when the stored JSON actually changes.

    Also mirrors start.greeting / global.prompt into agents.greeting /
    system_prompt — those columns are what the runtime reads today.
    A missing start/global node clears that column (no leftover stale text).
    """
    peeked = graph if graph is not None else await _peek_draft(agent_id, tenant_slug)
    if peeked is None:
        raise ValueError("nothing to publish — this agent has no workflow draft")
    warnings = await validate(peeked)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await _locked_agent(conn, agent_id, tenant_slug)
            candidate = graph if graph is not None else _as_graph(old["workflow_draft"])
            if candidate is None:
                raise ValueError("nothing to publish — this agent has no workflow draft")
            if not graphs_equivalent(candidate, peeked):
                warnings = await validate(candidate)

            current = _as_graph(old["workflow"])
            if graphs_equivalent(candidate, current):
                # Logic matches live, but canvas chrome (positions) may differ.
                # Write both columns so the editor's position compare clears;
                # no version row — conversation logic did not change.
                new_row = await conn.fetchrow(
                    """
                    UPDATE agents SET
                        workflow = $2::jsonb,
                        workflow_draft = $2::jsonb
                    WHERE id = $1
                    RETURNING config_version, slug
                    """,
                    agent_id, json.dumps(candidate),
                )
                version = await conn.fetchval(
                    "SELECT COALESCE(MAX(version), 0) FROM agent_workflow_versions WHERE agent_id = $1",
                    agent_id,
                )
                new = dict(new_row)
            else:
                payload = json.dumps(candidate)
                greeting, system_prompt = column_prompts(candidate)
                new_row = await conn.fetchrow(
                    """
                    UPDATE agents SET
                        workflow = $2::jsonb,
                        workflow_draft = $2::jsonb,
                        greeting = $3,
                        system_prompt = $4
                    WHERE id = $1
                    RETURNING *
                    """,
                    agent_id, payload, greeting, system_prompt,
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

    await cache.invalidate(agents_service.cache_key(tenant_slug, new["slug"]))
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
