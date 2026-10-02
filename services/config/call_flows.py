"""
Call-flow (IVR/OBD) CRUD + draft/publish/versions. `graph_draft` autosaves and
may be invalid; `graph` is what live calls walk.

Always use tenant_conn()/platform_conn(): FORCE RLS makes a bare pool read empty.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from libs.config_sdk.callflow import (
    CallFlowError,
    CallFlowValidationError as GraphInvalid,
    graph_warnings,
    parse_graph,
    starter_graph,
)
from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, db
from . import tenants as tenants_service

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


async def _agent_reference_errors(
    conn: Any, graph: dict[str, Any], tenant_id: Any,
) -> list[CallFlowError]:
    """Editor errors for `agent` nodes naming a deleted, inactive or foreign agent.

    Same predicate as get_published_for_runtime()'s agent_slugs query."""
    pairs = [
        (str(n.get("id") or ""), str((n.get("data") or {}).get("agent_id") or ""))
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("type") == "agent"
    ]

    errs: list[CallFlowError] = []
    # node ids keyed by the agent id they name, so one lookup covers repeats
    by_agent: dict[str, list[str]] = {}
    for node_id, agent_id in pairs:
        if not agent_id:
            continue  # _node_errors already reports an unset agent_id
        try:
            canonical = str(uuid.UUID(agent_id))
        except (ValueError, AttributeError, TypeError):
            canonical = None
        if canonical != agent_id:
            # Canonical form only: asyncpg rejects braces/urn (500) and the
            # runtime's agent_slugs lookup is exact-string (uppercase misses).
            errs.append(CallFlowError(
                "node", node_id, "agent_id",
                "This step no longer points at a real agent — pick one again.",
            ))
            continue
        by_agent.setdefault(agent_id, []).append(node_id)

    if by_agent:
        rows = await conn.fetch(
            "SELECT id FROM agents "
            " WHERE id = ANY($1::uuid[]) AND tenant_id = $2 "
            "   AND deleted_at IS NULL AND status = 'active'",
            list(by_agent), tenant_id,
        )
        live = {str(r["id"]) for r in rows}
        for agent_id, node_ids in by_agent.items():
            if agent_id in live:
                continue
            for node_id in node_ids:
                errs.append(CallFlowError(
                    "node", node_id, "agent_id",
                    "The agent this step hands the call to is no longer "
                    "available — it may have been deleted or deactivated. "
                    "Pick another.",
                ))
    return errs


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
    """platform_scoped: by-id read by a NULL-tenant caller before the tenant is known."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="call-flow-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM call_flows WHERE id = $1 AND deleted_at IS NULL", call_flow_id,
        )
    return _row(row)


def _runtime_cache_key(tenant_slug: str, call_flow_id: Any) -> str:
    return f"callflow:{tenant_slug}:{call_flow_id}"


async def get_published_for_runtime(tenant_slug: str, call_flow_id: Any) -> dict[str, Any] | None:
    """Runtime read behind GET /published for the conversation service.

    The caller is platform-scoped, so every query also filters tenant_id explicitly, not just via RLS."""
    key = _runtime_cache_key(tenant_slug, call_flow_id)
    cached = await cache.get_json(key)
    if cached is not None:
        return cached

    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        return None
    tenant_id = tenant["id"]

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT id, config_version, status, direction, graph FROM call_flows "
            " WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL "
            "   AND graph IS NOT NULL AND direction = 'inbound'",
            call_flow_id, tenant_id,
        )
        if row is None:
            return None
        graph = row["graph"]
        graph = json.loads(graph) if isinstance(graph, str) else graph

        agent_ids = [
            aid for n in (graph.get("nodes") or [])
            if n.get("type") == "agent" and (aid := (n.get("data") or {}).get("agent_id"))
        ]
        agent_slugs: dict[str, str] = {}
        if agent_ids:
            agent_rows = await conn.fetch(
                "SELECT id, slug FROM agents "
                " WHERE id = ANY($1::uuid[]) AND tenant_id = $2 "
                "   AND deleted_at IS NULL AND status = 'active'",
                agent_ids, tenant_id,
            )
            agent_slugs = {str(r["id"]): r["slug"] for r in agent_rows}

        start_node = next((n for n in graph.get("nodes") or [] if n.get("type") == "start"), None)
        tts_config_id = (start_node.get("data") or {}).get("tts_config_id") if start_node else None
        resolved_tts_config_id: str | None = None
        if tts_config_id:
            tts_row = await conn.fetchrow(
                "SELECT id FROM provider_configs WHERE id = $1 AND tenant_id = $2 AND role = 'tts'",
                tts_config_id, tenant_id,
            )
            if tts_row is not None:
                resolved_tts_config_id = str(tts_row["id"])

    payload = {
        "id": str(row["id"]),
        "tenant_slug": tenant_slug,
        "config_version": row["config_version"],
        "status": row["status"],
        "direction": row["direction"],
        "graph": graph,
        "agent_slugs": agent_slugs,
        "resolved_tts_config_id": resolved_tts_config_id,
    }
    await cache.set_json(key, payload)
    return payload


async def _invalidate_runtime_cache(call_flow_id: Any, tenant_id: Any) -> None:
    tenant = await tenants_service.get_tenant_by_id(tenant_id)
    if tenant is not None:
        await cache.invalidate(_runtime_cache_key(tenant["slug"], call_flow_id))


async def invalidate_runtime_caches_naming_agent(
    tenant_id: Any, tenant_slug: str, agent_id: Any,
) -> None:
    """Drop cached runtime payloads of published flows naming this agent (its slug is cached)."""
    needle = json.dumps([{"type": "agent", "data": {"agent_id": str(agent_id)}}])
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT id FROM call_flows "
            " WHERE tenant_id = $1 AND deleted_at IS NULL AND graph IS NOT NULL "
            "   AND graph->'nodes' @> $2::jsonb",
            tenant_id, needle,
        )
    if rows:
        await cache.invalidate(*(_runtime_cache_key(tenant_slug, r["id"]) for r in rows))


async def create_call_flow(
    *, tenant_id: Any, slug: str, name: str, description: str = "",
    direction: str = "inbound", clone_from_id: Any | None = None,
    graph: dict[str, Any] | None = None,
    user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """Graph from a clone, a caller scaffold, or the starter; published if valid, else a draft.

    Cloning goes through tenant_conn, so another tenant's clone_from_id is not-found."""
    if clone_from_id is not None:
        source = await get_call_flow(clone_from_id)
        if source is None:
            raise LookupError(f"call_flow {clone_from_id} not found")
        graph = source["graph"] or source["graph_draft"] or starter_graph()
    elif graph is None:
        graph = starter_graph()

    # Step-picker scaffolds are expected to have blanks, so invalid lands as a draft.
    try:
        await validate(graph)
        publishable = True
    except CallFlowValidationError:
        publishable = False

    graph_json = json.dumps(graph)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # A clone may name an agent deleted/deactivated since; land it as a draft.
        if publishable and await _agent_reference_errors(conn, graph, tenant_id):
            publishable = False
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
    await _invalidate_runtime_cache(call_flow_id, result["tenant_id"])
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
            " WHERE id = $1 AND deleted_at IS NULL RETURNING id, tenant_id", call_flow_id,
        )
        if row is None:
            return False
        await conn.execute("UPDATE agents SET call_flow_id = NULL WHERE call_flow_id = $1", call_flow_id)
        await audit.write_audit(
            conn, entity_type="call_flow", entity_id=call_flow_id, action="deleted",
            user_id=user_id, user_email=user_email,
        )
    await _invalidate_runtime_cache(call_flow_id, row["tenant_id"])
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
            "SELECT config_version, tenant_id FROM call_flows "
            " WHERE id = $1 AND deleted_at IS NULL", call_flow_id,
        )
        if current is None:
            raise LookupError(f"call_flow {call_flow_id} not found")
        # Before anything is written: an `agent` node naming an agent that
        # cannot answer would publish clean here and then drop a live call.
        agent_errs = await _agent_reference_errors(conn, graph, current["tenant_id"])
        if agent_errs:
            raise CallFlowValidationError(agent_errs)
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
        # audit_log.action allows only created/updated/deleted.
        await audit.write_audit(
            conn, entity_type="call_flow_publish", entity_id=call_flow_id, action="updated",
            user_id=user_id, user_email=user_email,
            new_value={"version": next_version, "note": note},
        )
    result = _row(row)
    await _invalidate_runtime_cache(call_flow_id, result["tenant_id"])
    return result


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
