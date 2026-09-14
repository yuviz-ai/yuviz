"""
Agent CRUD — same cache-aside + audited-mutation pattern as tenants.py.

get_agent() is keyed by (tenant_slug, agent_slug) rather than a bare id,
because that's what the hot path actually has: the WebSocket path is
`/<agent>/<uuid>`, and the tenant is resolved from the same connection
context — nobody holds a UUID before the call starts. get_agent_by_id()
exists for the Admin UI's edit-by-id flow, where the id is already known.

Prompt sync (phase until Conversation reads agents.workflow):
- Runtime still speaks agents.greeting / system_prompt.
- PATCH of those fields mirrors into start/global and appends a version.
- Missing start/global on PATCH is a 400, not a silent no-op.
- publish()/create derive the columns from the graph.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from libs.config_sdk.workflow import graphs_equivalent, starter_graph

from . import audit, cache, db

# `workflow` is absent — only publish/create may write a validated graph
# (except the greeting/system_prompt mirror sync in update_agent).
_UPDATABLE_FIELDS = {
    "name", "greeting", "system_prompt", "goodbye_grace_ms", "language",
    "stt_config_id", "llm_config_id", "tts_config_id",
    "transfer_type", "transfer_destination", "queue_id", "escalation_threshold",
    "caller_id_policy", "platform_did", "custom_caller_id",
    "transfer_waiting_experience",
    "end_call_prompt", "transfer_prompt",
    "farewell_message", "transfer_announcement",
    "status", "max_call_duration_s",
}

_JSON_COLUMNS = ("workflow", "workflow_draft")


def cache_key(tenant_slug: str, agent_slug: str) -> str:
    return f"agent:{tenant_slug}:{agent_slug}"


def _row(row: Any) -> dict[str, Any]:
    """Decode JSONB columns on an agents row (see db.json_col)."""
    out = dict(row)
    for column in _JSON_COLUMNS:
        if column in out:
            out[column] = db.json_col(out[column])
    return out


def _audit_view(row: dict[str, Any]) -> dict[str, Any]:
    """Strip graph columns from ordinary agent audits (publish records them)."""
    return {k: v for k, v in row.items() if k not in _JSON_COLUMNS}


def _public_agent(row: dict[str, Any]) -> dict[str, Any]:
    """Published workflow stays on the agent GET/cache payload so call-setup
    can carry it into RuntimeConfig. Draft is editor-only until draft testing."""
    out = dict(row)
    out.pop("workflow_draft", None)
    return out


def _graph_node_count(graph: Any) -> int | None:
    if not isinstance(graph, dict):
        return None
    nodes = graph.get("nodes")
    return len(nodes) if isinstance(nodes, list) else None


def _list_workflow_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Lean list metadata for the editor index — never full graph bodies."""
    wf = raw.get("workflow") if isinstance(raw.get("workflow"), dict) else None
    draft = raw.get("workflow_draft") if isinstance(raw.get("workflow_draft"), dict) else None
    has_wf = wf is not None
    has_draft = draft is not None
    diverged = bool(has_wf and has_draft and not graphs_equivalent(wf, draft))
    count_src = draft if (has_draft and (not has_wf or diverged)) else wf
    return {
        "has_workflow": has_wf,
        "has_workflow_draft": has_draft,
        "workflow_diverged": diverged,
        "workflow_node_count": _graph_node_count(count_src),
    }


def _coerce_prompt(value: Any) -> str:
    return "" if value is None else str(value)


def _require_prompt_nodes(graph: dict[str, Any], fields: dict[str, Any]) -> None:
    types = {n.get("type") for n in (graph.get("nodes") or [])}
    if "greeting" in fields and "start" not in types:
        raise ValueError("cannot update greeting: this agent's workflow has no start node")
    if "system_prompt" in fields and "global" not in types:
        raise ValueError(
            "cannot update system_prompt: this agent's workflow has no always-on (global) node"
        )


def _mirror_prompts_into_graph(
    graph: dict[str, Any] | None, fields: dict[str, Any],
) -> dict[str, Any] | None:
    """Copy greeting/system_prompt into start/global nodes when those fields change."""
    if graph is None or ("greeting" not in fields and "system_prompt" not in fields):
        return graph
    _require_prompt_nodes(graph, fields)
    nodes: list[dict[str, Any]] = []
    for raw in graph.get("nodes") or []:
        node = dict(raw)
        data = dict(node.get("data") or {})
        if node.get("type") == "global" and "system_prompt" in fields:
            data["prompt"] = _coerce_prompt(fields["system_prompt"])
        if node.get("type") == "start" and "greeting" in fields:
            data["greeting"] = _coerce_prompt(fields["greeting"])
        node["data"] = data
        nodes.append(node)
    return {**graph, "nodes": nodes}


async def get_agent(tenant_slug: str, agent_slug: str) -> dict[str, Any] | None:
    cached = await cache.get_json(cache_key(tenant_slug, agent_slug))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
        "WHERE t.slug = $1 AND a.slug = $2 AND a.deleted_at IS NULL AND t.deleted_at IS NULL",
        tenant_slug, agent_slug,
    )
    if row is None:
        return None

    result = _public_agent(_row(row))
    await cache.set_json(cache_key(tenant_slug, agent_slug), result)
    return result


async def get_agent_by_id(agent_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND deleted_at IS NULL", agent_id,
    )
    return _public_agent(_row(row)) if row is not None else None


async def list_agents(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT * FROM agents WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
        tenant_id,
    )
    # List stays lean: badges/step counts only. Full graphs stay on GET
    # (published) and /workflow (draft+live); Conversation prewarm uses list.
    out = []
    for row in rows:
        raw = _row(row)
        agent = _public_agent(raw)
        agent.pop("workflow", None)
        agent.update(_list_workflow_fields(raw))
        out.append(agent)
    return out


_PROVIDER_ROLE_BY_FIELD = {
    "stt_config_id": "stt", "llm_config_id": "llm", "tts_config_id": "tts",
}

# macos/kokoro/deepgram all fall back to a sensible default voice when
# provider_configs.voice is unset (see ai_provider_manager.py's
# _make_macos_tts/_make_kokoro_tts/_make_deepgram_tts) — only elevenlabs has
# no safe fallback (cfg.voice or "" — an empty voice_id, which fails at
# call time with an opaque ElevenLabs API error). Checked here instead, so
# "connected but never picked a voice" is a clear save-time error.
_TTS_ENGINES_REQUIRING_VOICE = {"elevenlabs"}


async def _validate_provider_assignments(conn: Any, tenant_id: Any, fields: dict[str, Any]) -> None:
    """FK existence alone lets stt_config_id point at another tenant's
    provider, or at a real provider_configs row with the wrong role (e.g.
    an llm engine assigned as tts_config_id) — either silently breaks the
    agent at call time rather than at config time. Checked here instead of
    relying on the caller, so both create_agent() and update_agent() get
    the same guarantee."""
    for field, expected_role in _PROVIDER_ROLE_BY_FIELD.items():
        config_id = fields.get(field)
        if config_id is None:
            continue
        # UUID-format check before the query: a malformed (non-UUID-shaped)
        # string reaching asyncpg's parameter binding raises DataError, which
        # is neither a ValueError nor a LookupError — app.py has no handler
        # for it, so it would otherwise surface as a raw, undetailed 500
        # instead of a clean 400. Same discipline as deps.py's
        # validate_id_exists() for other id fields in request bodies.
        try:
            uuid.UUID(config_id)
        except (ValueError, TypeError):
            raise ValueError(f"{field}={config_id!r} is not a valid id") from None
        row = await conn.fetchrow(
            "SELECT tenant_id, role, engine, voice FROM provider_configs WHERE id = $1", config_id,
        )
        if row is None:
            raise ValueError(f"{field}={config_id!r} does not exist")
        if row["tenant_id"] != tenant_id:
            raise ValueError(f"{field}={config_id!r} belongs to a different tenant")
        if row["role"] != expected_role:
            raise ValueError(f"{field}={config_id!r} has role {row['role']!r}, expected {expected_role!r}")
        if row["engine"] in _TTS_ENGINES_REQUIRING_VOICE and not row["voice"]:
            raise ValueError(
                f"{field}={config_id!r} is a {row['engine']} provider with no voice selected — "
                "pick a voice for it before assigning it to an agent"
            )


async def create_agent(
    *,
    tenant_id: Any,
    slug: str,
    name: str,
    greeting: str = "",
    system_prompt: str = "",
    stt_config_id: Any | None = None,
    llm_config_id: Any | None = None,
    tts_config_id: Any | None = None,
    workflow: dict[str, Any] | None = None,
    tenant_slug: str | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Create agent + published starter workflow in one transaction.

    Caller-supplied `workflow` is validated like publish; None/{} seed
    starter_graph() from greeting/system_prompt. Starter shape leaves
    tools/KB empty on purpose — Conversation treats single-stage graphs as
    policy-driven (agent_tool_policies / agent_knowledge_bases) until the
    editor authors a multi-node graph.
    """
    # Deferred import: workflows.py imports this module for its cache key.
    from .workflows import append_version, column_prompts, validate

    graph = workflow if workflow else starter_graph(greeting, system_prompt)
    await validate(graph)
    greeting, system_prompt = column_prompts(graph)
    graph_json = json.dumps(graph)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _validate_provider_assignments(
                conn, tenant_id,
                {"stt_config_id": stt_config_id, "llm_config_id": llm_config_id, "tts_config_id": tts_config_id},
            )
            row = await conn.fetchrow(
                "INSERT INTO agents "
                "(tenant_id, slug, name, greeting, system_prompt, "
                "stt_config_id, llm_config_id, tts_config_id, workflow, workflow_draft) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $9::jsonb) RETURNING *",
                tenant_id, slug, name, greeting, system_prompt,
                stt_config_id, llm_config_id, tts_config_id, graph_json,
            )
            result = _row(row)
            await append_version(
                conn, result["id"], graph_json,
                user_id=user_id, note="created with the agent",
            )
            await audit.write_audit(
                conn,
                entity_type="agent",
                entity_id=result["id"],
                action="created",
                user_id=user_id,
                user_email=user_email,
                new_value=_audit_view(result),
            )
    public = _public_agent(result)
    if tenant_slug is not None:
        # Warm the cache immediately rather than leaving it for the agent's
        # first real call to populate lazily — same reasoning, and the same
        # real live-call failure this exact gap already caused, as
        # phone_numbers.create_phone_number()'s identical fix (see project
        # memory 2026-07-13). Optional (not required) because most existing
        # callers only have tenant_id on hand; the REST router (the actual
        # live-usage path) does have tenant_slug and passes it.
        await get_agent(tenant_slug, slug)
    return public


async def update_agent(
    agent_id: Any,
    *,
    tenant_slug: str,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """tenant_slug is required so the correct cache key can be invalidated —
    it's not derivable from agent_id alone without an extra query, and the
    caller (Admin UI / API layer) already has it from the request context."""
    if not fields:
        raise ValueError("update_agent() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_agent() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE OF a is two fixes in one: it locks the agent row for
            # the rest of this transaction (so a concurrent update can't read
            # a stale "old" value for the audit log — see project memory's
            # audit-race note), and the join against tenants scopes the
            # lookup by tenant_slug — an agent_id that exists but belongs to
            # a *different* tenant is indistinguishable from "doesn't exist"
            # to this caller. Previously this was scoped by agent_id alone,
            # which let any tenant's URL path update or delete any other
            # tenant's agent by id (cross-tenant hijack).
            old_row = await conn.fetchrow(
                "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
                "WHERE a.id = $1 AND t.slug = $2 FOR UPDATE OF a",
                agent_id, tenant_slug,
            )
            if old_row is None:
                raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
            old = _row(old_row)
            await _validate_provider_assignments(conn, old["tenant_id"], fields)

            set_fields = dict(fields)
            if "greeting" in set_fields:
                set_fields["greeting"] = _coerce_prompt(set_fields["greeting"])
            if "system_prompt" in set_fields:
                set_fields["system_prompt"] = _coerce_prompt(set_fields["system_prompt"])

            mirrored_graph = False
            if old.get("workflow") is not None and (
                "greeting" in fields or "system_prompt" in fields
            ):
                synced_wf = _mirror_prompts_into_graph(old["workflow"], set_fields)
                if synced_wf != old["workflow"]:
                    set_fields["workflow"] = json.dumps(synced_wf)
                    mirrored_graph = True
                draft_src = (
                    old["workflow_draft"]
                    if old.get("workflow_draft") is not None
                    else old["workflow"]
                )
                synced_draft = _mirror_prompts_into_graph(draft_src, set_fields)
                if synced_draft != draft_src:
                    set_fields["workflow_draft"] = json.dumps(synced_draft)
                    mirrored_graph = True

            columns = list(set_fields.keys())
            set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
            new_row = await conn.fetchrow(
                f"UPDATE agents SET {set_clause} WHERE id = $1 RETURNING *",
                agent_id, *(set_fields[col] for col in columns),
            )
            new = _row(new_row)

            if mirrored_graph and isinstance(set_fields.get("workflow"), str):
                from .workflows import append_version
                await append_version(
                    conn, agent_id, set_fields["workflow"],
                    user_id=user_id, note="mirrored greeting/system_prompt",
                )

            # Mirror mutates the live graph — keep graphs in this audit row.
            old_audit = old if mirrored_graph else _audit_view(old)
            new_audit = new if mirrored_graph else _audit_view(new)
            await audit.write_audit(
                conn,
                entity_type="agent",
                entity_id=agent_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value=old_audit,
                new_value=new_audit,
            )

    await cache.invalidate(cache_key(tenant_slug, old["slug"]))
    return _public_agent(new)


async def soft_delete_agent(
    agent_id: Any,
    *,
    tenant_slug: str,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # See update_agent()'s comment — same tenant-scoping + row-lock fix.
            old_row = await conn.fetchrow(
                "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
                "WHERE a.id = $1 AND t.slug = $2 FOR UPDATE OF a",
                agent_id, tenant_slug,
            )
            if old_row is None:
                raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
            old = _row(old_row)

            await conn.execute("UPDATE agents SET deleted_at = now() WHERE id = $1", agent_id)
            await audit.write_audit(
                conn,
                entity_type="agent",
                entity_id=agent_id,
                action="deleted",
                user_id=user_id,
                user_email=user_email,
                old_value=_audit_view(old),
            )

    await cache.invalidate(cache_key(tenant_slug, old["slug"]))
