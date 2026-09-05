"""
Agent CRUD — same cache-aside + audited-mutation pattern as tenants.py.

get_agent() is keyed by (tenant_slug, agent_slug) rather than a bare id,
because that's what the hot path actually has: the WebSocket path is
`/<agent>/<uuid>`, and the tenant is resolved from the same connection
context — nobody holds a UUID before the call starts. get_agent_by_id()
exists for the Admin UI's edit-by-id flow, where the id is already known.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from libs.config_sdk.workflow import starter_graph

from . import audit, cache, db

# `workflow` is absent — only publish/create may write a validated graph.
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


def _cache_key(tenant_slug: str, agent_slug: str) -> str:
    return f"agent:{tenant_slug}:{agent_slug}"


def _row(row: Any) -> dict[str, Any]:
    """Decode JSONB columns on an agents row (see db.json_col)."""
    out = dict(row)
    for column in _JSON_COLUMNS:
        if column in out:
            out[column] = db.json_col(out[column])
    return out


async def get_agent(tenant_slug: str, agent_slug: str) -> dict[str, Any] | None:
    cached = await cache.get_json(_cache_key(tenant_slug, agent_slug))
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

    result = _row(row)
    await cache.set_json(_cache_key(tenant_slug, agent_slug), result)
    return result


async def get_agent_by_id(agent_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND deleted_at IS NULL", agent_id,
    )
    return _row(row) if row is not None else None


async def list_agents(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT * FROM agents WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
        tenant_id,
    )
    return [_row(row) for row in rows]


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
    starter_graph() from greeting/system_prompt.
    """
    # Deferred import: workflows.py imports this module for its cache key.
    from .workflows import append_version, validate

    graph = workflow if workflow else starter_graph(greeting, system_prompt)
    validate(graph)
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
                new_value=result,
            )
    if tenant_slug is not None:
        # Warm the cache immediately rather than leaving it for the agent's
        # first real call to populate lazily — same reasoning, and the same
        # real live-call failure this exact gap already caused, as
        # phone_numbers.create_phone_number()'s identical fix (see project
        # memory 2026-07-13). Optional (not required) because most existing
        # callers only have tenant_id on hand; the REST router (the actual
        # live-usage path) does have tenant_slug and passes it.
        await get_agent(tenant_slug, slug)
    return result


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

            columns = list(fields.keys())
            set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
            new_row = await conn.fetchrow(
                f"UPDATE agents SET {set_clause} WHERE id = $1 RETURNING *",
                agent_id, *(fields[col] for col in columns),
            )
            new = _row(new_row)

            await audit.write_audit(
                conn,
                entity_type="agent",
                entity_id=agent_id,
                action="updated",
                user_id=user_id,
                user_email=user_email,
                old_value=old,
                new_value=new,
            )

    await cache.invalidate(_cache_key(tenant_slug, old["slug"]))
    return new


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
                old_value=old,
            )

    await cache.invalidate(_cache_key(tenant_slug, old["slug"]))
