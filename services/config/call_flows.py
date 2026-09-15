"""
Call-flow (IVR/OBD) CRUD + draft/publish/versions.

Mirrors workflows.py's draft/publish split — `graph_draft` autosaves and may
be invalid, `graph` is the published graph a live call walks, and
call_flow_versions is append-only publish history — but for an object with
its own identity: a flow is tenant-scoped, named, and may front several
agents (agents.call_flow_id), rather than being one agent's own column.

Every statement goes through libs.tenancy's tenant_conn()/platform_conn(),
never a bare pool call — call_flows and call_flow_versions both carry FORCE
ROW LEVEL SECURITY (database/rls.sql), so an unscoped connection would read
back as empty rather than failing loudly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from libs.config_sdk.callflow import (
    CallFlowError,
    CallFlowValidationError as GraphInvalid,
    graph_warnings,
    parse_graph,
    starter_graph,
)
from libs.tenancy import platform_conn, tenant_conn

from . import audit, db

_UPDATABLE_FIELDS = {"name", "description", "status", "direction"}
_JSON_COLUMNS = ("graph", "graph_draft")


class CallFlowValidationError(Exception):
    """Structured per-node/per-edge errors for the editor."""

    def __init__(self, errors: list[CallFlowError]) -> None:
        self.errors = errors
        super().__init__("call flow is not valid")


class StaleDraft(Exception):
    """Draft PUT lost a race with a publish (config_version moved)."""


def _row(row: Any) -> dict[str, Any] | None:
    """asyncpg returns JSONB as a string; every caller wants the object."""
    if row is None:
        return None
    out = dict(row)
    for col in _JSON_COLUMNS:
        if isinstance(out.get(col), str):
            out[col] = json.loads(out[col])
    return out


def _validate_sync(graph: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        parsed = parse_graph(graph)
    except GraphInvalid as exc:
        raise CallFlowValidationError(exc.errors) from None
    return [w.to_dict() for w in graph_warnings(parsed)]


async def validate(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """CPU-bound parse + warnings off the event loop (Config also serves call setup)."""
    return await asyncio.to_thread(_validate_sync, graph)


async def list_call_flows(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT id, tenant_id, slug, name, description, status, direction, config_version, "
            "       created_at, updated_at, "
            "       (graph IS NOT NULL) AS is_published, "
            "       (graph_draft IS NOT NULL) AS has_draft "
            "  FROM call_flows WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def get_call_flow(call_flow_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """platform_scoped is for a NULL-tenant caller doing a by-id read before
    the target tenant is known — the Tier 3 pattern in docs/rls-tenant-isolation.md."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="call-flow-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM call_flows WHERE id = $1 AND deleted_at IS NULL", call_flow_id,
        )
    return _row(row)


async def create_call_flow(
    *, tenant_id: Any, slug: str, name: str, description: str = "",
    direction: str = "inbound", clone_from_id: Any | None = None,
    graph: dict[str, Any] | None = None,
    user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """Its graph comes from one of three places, in order: a clone of another
    flow, a caller-supplied scaffold (what the builder's step picker
    produces), or the built-in starter. Cloning reads through tenant_conn, so
    a clone_from_id belonging to another tenant resolves to nothing and is
    reported as not-found rather than silently copied across the boundary.

    A valid graph is published on creation; an incomplete scaffold lands as a
    draft (see below).
    """
    if clone_from_id is not None:
        source = await get_call_flow(clone_from_id)
        if source is None:
            raise LookupError(f"call_flow {clone_from_id} not found")
        graph = source["graph"] or source["graph_draft"] or starter_graph()
    elif graph is None:
        graph = starter_graph()

    # A scaffold from the builder's step picker is expected to have blanks —
    # "hand to an AI agent" can't name the agent until you pick one, and the
    # picker is not the place to do that. So an invalid graph lands as a
    # draft (unpublished, nothing points at it, the canvas shows what to
    # fix) instead of failing creation outright. The starter and any clone
    # are always valid and still publish immediately, which is what keeps an
    # attachable flow from ever answering a call with nothing.
    try:
        await validate(graph)
        publishable = True
    except CallFlowValidationError:
        publishable = False

    graph_json = json.dumps(graph)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO call_flows (tenant_id, slug, name, description, direction, graph, graph_draft) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb) RETURNING *",
            tenant_id, slug, name, description, direction,
            graph_json if publishable else None, graph_json,
        )
        result = _row(row)
        if publishable:
            await conn.execute(
                "INSERT INTO call_flow_versions (call_flow_id, version, graph, published_by, note) "
                "VALUES ($1, 1, $2::jsonb, $3, $4)",
                result["id"], graph_json, user_id, "created with the flow",
            )
        await audit.write_audit(
            conn, entity_type="call_flow", entity_id=result["id"], action="created",
            user_id=user_id, user_email=user_email,
            new_value={"slug": slug, "name": name, "direction": direction,
                       "published": publishable,
                       "cloned_from": str(clone_from_id) if clone_from_id else None},
        )
    return result


async def update_call_flow(
    call_flow_id: Any, *, user_id: Any | None = None, user_email: str | None = None, **fields: Any,
) -> dict[str, Any] | None:
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return await get_call_flow(call_flow_id)

    cols = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old = await conn.fetchrow("SELECT * FROM call_flows WHERE id = $1 AND deleted_at IS NULL", call_flow_id)
        if old is None:
            return None
        row = await conn.fetchrow(
            f"UPDATE call_flows SET {cols}, updated_at = now() WHERE id = $1 RETURNING *",
            call_flow_id, *fields.values(),
        )
        result = _row(row)
        await audit.write_audit(
            conn, entity_type="call_flow", entity_id=call_flow_id, action="updated",
            user_id=user_id, user_email=user_email,
            old_value={k: _row(old)[k] for k in fields}, new_value=fields,
        )
    return result


async def delete_call_flow(
    call_flow_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> bool:
    """Soft delete. agents.call_flow_id is ON DELETE SET NULL, but a soft
    delete never fires that — so detach pointing agents explicitly, or they
    would keep resolving a flow the tenant believes is gone."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "UPDATE call_flows SET deleted_at = now() "
            " WHERE id = $1 AND deleted_at IS NULL RETURNING id", call_flow_id,
        )
        if row is None:
            return False
        await conn.execute("UPDATE agents SET call_flow_id = NULL WHERE call_flow_id = $1", call_flow_id)
        await audit.write_audit(
            conn, entity_type="call_flow", entity_id=call_flow_id, action="deleted",
            user_id=user_id, user_email=user_email,
        )
    return True


async def save_draft(call_flow_id: Any, graph: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
    """Autosave. Deliberately does NOT validate: a half-drawn flow must be
    savable. expected_version fences a late PUT from clobbering a publish."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        current = await conn.fetchrow(
            "SELECT config_version FROM call_flows WHERE id = $1 AND deleted_at IS NULL", call_flow_id,
        )
        if current is None:
            raise LookupError(f"call_flow {call_flow_id} not found")
        if expected_version is not None and current["config_version"] != expected_version:
            raise StaleDraft(
                f"draft is against version {expected_version}, flow is now at {current['config_version']}",
            )
        row = await conn.fetchrow(
            "UPDATE call_flows SET graph_draft = $2::jsonb, updated_at = now() WHERE id = $1 RETURNING *",
            call_flow_id, json.dumps(graph),
        )
    return _row(row)


async def publish(
    call_flow_id: Any, graph: dict[str, Any], *,
    user_id: Any | None = None, user_email: str | None = None, note: str | None = None,
) -> dict[str, Any]:
    """Validates, then writes the live graph + a version row in one
    transaction. Raises CallFlowValidationError with editor-facing errors."""
    await validate(graph)
    graph_json = json.dumps(graph)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        current = await conn.fetchrow(
            "SELECT config_version FROM call_flows WHERE id = $1 AND deleted_at IS NULL", call_flow_id,
        )
        if current is None:
            raise LookupError(f"call_flow {call_flow_id} not found")
        next_version = current["config_version"] + 1
        row = await conn.fetchrow(
            "UPDATE call_flows SET graph = $2::jsonb, graph_draft = $2::jsonb, "
            "       config_version = $3, updated_at = now() WHERE id = $1 RETURNING *",
            call_flow_id, graph_json, next_version,
        )
        await conn.execute(
            "INSERT INTO call_flow_versions (call_flow_id, version, graph, published_by, note) "
            "VALUES ($1, $2, $3::jsonb, $4, $5)",
            call_flow_id, next_version, graph_json, user_id, note,
        )
        # action is constrained to created/updated/deleted (audit_log_action_check);
        # a publish is recorded the way workflows.py records one — "updated"
        # under its own entity_type, not a fourth action value.
        await audit.write_audit(
            conn, entity_type="call_flow_publish", entity_id=call_flow_id, action="updated",
            user_id=user_id, user_email=user_email,
            new_value={"version": next_version, "note": note},
        )
    return _row(row)


async def list_versions(call_flow_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT v.version, v.published_at, v.note, u.email AS published_by_email "
            "  FROM call_flow_versions v LEFT JOIN users u ON u.id = v.published_by "
            " WHERE v.call_flow_id = $1 ORDER BY v.version DESC LIMIT 50",
            call_flow_id,
        )
    return [dict(r) for r in rows]


async def get_version_graph(call_flow_id: Any, version: int) -> dict[str, Any] | None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT graph FROM call_flow_versions WHERE call_flow_id = $1 AND version = $2",
            call_flow_id, version,
        )
    if row is None:
        return None
    graph = row["graph"]
    return json.loads(graph) if isinstance(graph, str) else graph
