"""
Agent CRUD — cache-aside + audited mutations. get_agent() is keyed by slugs
because that's all the call hot path has.

Prompt sync (phase until Conversation reads agents.workflow):
- Runtime still speaks agents.greeting / system_prompt.
- PATCH of those fields mirrors into start/global and appends a version.
- Missing start/global on PATCH is a 400, not a silent no-op.
- publish()/create derive the columns from the graph.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Callable

from libs.config_sdk.languages import (
    DEEPGRAM_MULTI,
    LANGUAGES,
    agent_supported_languages,
    deepgram_supports_multi,
    language_name,
    normalize_language,
    tts_languages,
    tts_model_of,
)
from libs.config_sdk.workflow import graphs_equivalent, starter_graph
from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, call_flows, db
from .provider_configs import require_usable_tts_voice
from .system_prompt import check_prompt_structure

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
    # Which call flow (if any) answers ahead of this agent — see call_flows.py.
    "call_flow_id",
    "supported_languages", "tts_config_by_language", "greeting_by_language",
}

_GRAPH_COLUMNS = ("workflow", "workflow_draft")
_JSON_COLUMNS = (*_GRAPH_COLUMNS, "tts_config_by_language", "greeting_by_language")

# Undo slot: the prompt before the last accepted fix, and the hash of the
# prompt that fix wrote. Never leaves this module (audit rows, API payloads).
_SLOT_COLUMNS = ("prompt_undo_previous", "prompt_undo_accepted_sha256")

_PROMPT_SHA_SQL = "encode(sha256(convert_to(coalesce(system_prompt,''),'UTF8')),'hex')"


def cache_key(tenant_slug: str, agent_slug: str) -> str:
    return f"agent:{tenant_slug}:{agent_slug}"


def _row(row: Any) -> dict[str, Any]:
    """Decode JSONB columns on an agents row (see db.json_col)."""
    out = dict(row)
    for column in _JSON_COLUMNS:
        if column in out:
            out[column] = db.json_col(out[column])
    return out


def _strip_slot(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _SLOT_COLUMNS}


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _audit_view(row: dict[str, Any]) -> dict[str, Any]:
    """Strip graph columns from ordinary agent audits (publish records them)."""
    return {k: v for k, v in _strip_slot(row).items() if k not in _GRAPH_COLUMNS}


def _public_agent(row: dict[str, Any]) -> dict[str, Any]:
    """Published workflow stays on the agent GET/cache payload so call-setup
    can carry it into RuntimeConfig. Draft is editor-only until draft testing.
    can_undo and prompt_fixable are computed from the raw row before the slot
    is stripped."""
    prompt = row.get("system_prompt") or ""
    out = _strip_slot(row)
    out.pop("workflow_draft", None)
    out["can_undo"] = (
        row.get("prompt_undo_previous") is not None
        and _sha256_hex(prompt) == row.get("prompt_undo_accepted_sha256")
    )
    out["prompt_fixable"] = check_prompt_structure(prompt)
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
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
            "WHERE t.slug = $1 AND a.slug = $2 AND a.deleted_at IS NULL AND t.deleted_at IS NULL",
            tenant_slug, agent_slug,
        )
    if row is None:
        return None

    result = _public_agent(_row(row))
    await cache.set_json(cache_key(tenant_slug, agent_slug), result)
    return result


async def get_agent_by_id(agent_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="agents-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agents WHERE id = $1 AND deleted_at IS NULL", agent_id,
        )
    return _public_agent(_row(row)) if row is not None else None


async def list_agents(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
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


async def _validate_provider_assignments(conn: Any, tenant_id: Any, fields: dict[str, Any]) -> None:
    """Reject provider ids from another tenant or with the wrong role (FK alone allows both)."""
    for field, expected_role in _PROVIDER_ROLE_BY_FIELD.items():
        config_id = fields.get(field)
        if config_id is None:
            continue
        # A malformed id would raise asyncpg DataError (an unhandled 500).
        try:
            uuid.UUID(config_id)
        except (ValueError, TypeError):
            raise ValueError(f"{field}={config_id!r} is not a valid id") from None
        # FOR SHARE: serializes with a concurrent delete's FOR UPDATE.
        row = await conn.fetchrow(
            "SELECT tenant_id, role, engine, voice FROM provider_configs "
            "WHERE id = $1 AND deleted_at IS NULL FOR SHARE", config_id,
        )
        if row is None:
            raise ValueError(f"{field} not found")
        # tenant_id may be a str (cache hit) or UUID; compare as strings.
        if str(row["tenant_id"]) != str(tenant_id):
            raise ValueError(f"{field} not found")
        if row["role"] != expected_role:
            raise ValueError(f"{field}={config_id!r} has role {row['role']!r}, expected {expected_role!r}")
        require_usable_tts_voice(field, config_id, row["engine"], row["voice"])


_LANGUAGE_FIELDS = (
    "language", "supported_languages", "tts_config_by_language", "greeting_by_language",
    "tts_config_id", "stt_config_id",
)
_MAX_GREETING_CHARS = 2000
# BCP-47-ish: "en", "hi", "en-US", "zh-Hant-TW". Providers get a per-engine normalised form.
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def _check_language_tag(value: Any) -> None:
    """Shape check for agents.language on write. Not the registry: a single-language
    agent may use any language its providers support (e.g. "nl-BE")."""
    if value is None:
        return
    if not isinstance(value, str) or not _LANGUAGE_TAG_RE.match(value.strip()):
        raise ValueError(f"language {value!r} is not a language code (e.g. 'en', 'hi', 'en-US')")


async def _tts_row(conn: Any, config_id: Any, tenant_id: Any) -> Any:
    """A provider row of this tenant; another tenant's row reads as absent."""
    return await conn.fetchrow(
        "SELECT id, tenant_id, name, role, engine, model, voice, extra FROM provider_configs "
        "WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL FOR SHARE", config_id, tenant_id,
    )


async def _validate_languages(
    conn: Any, tenant_id: Any, merged: dict[str, Any], *, lock_defaults: bool = False,
) -> dict[str, Any]:
    """Validate the multilingual fields as one unit and return their normalised column
    values. `merged` is the agent's state after this write (old row overlaid with fields).

    Override ids are always checked same-tenant. Language coverage is only checked
    when supported_languages is set, so single-language agents save exactly as before.
    Raises ValueError (-> 400) naming the language and provider on any problem."""
    supported_raw = merged.get("supported_languages") or []
    if not isinstance(supported_raw, (list, tuple)) or not all(isinstance(c, str) for c in supported_raw):
        raise ValueError("supported_languages must be a list of language codes")
    supported: tuple[str, ...] = ()
    default = None
    if supported_raw:
        for code in [merged.get("language"), *supported_raw]:
            if code is not None and normalize_language(code) not in LANGUAGES:
                raise ValueError(
                    f"language {code!r} is not supported — choose from {', '.join(sorted(LANGUAGES))}"
                )
        default = normalize_language(merged.get("language")) or normalize_language(supported_raw[0])
        supported = agent_supported_languages(default, supported_raw)

    overrides_raw = merged.get("tts_config_by_language") or {}
    if not isinstance(overrides_raw, dict):
        raise ValueError("tts_config_by_language must be an object of {language: tts_config_id}")
    overrides: dict[str, str] = {}
    for lang_raw, config_id in overrides_raw.items():
        lang = normalize_language(lang_raw)
        if lang not in LANGUAGES:
            raise ValueError(f"tts_config_by_language has an unknown language {lang_raw!r}")
        if supported and lang not in supported:
            raise ValueError(f"tts_config_by_language has a voice for {lang_raw!r}, which is not in supported_languages")
        if config_id in (None, ""):
            continue
        try:
            uuid.UUID(str(config_id))
        except ValueError:
            raise ValueError(f"tts_config_by_language[{lang!r}]={config_id!r} is not a valid id") from None
        row = await _tts_row(conn, str(config_id), tenant_id)
        if row is None:
            raise ValueError(f"tts_config_by_language[{lang!r}] not found")
        if row["role"] != "tts":
            raise ValueError(f"tts_config_by_language[{lang!r}]={config_id!r} has role {row['role']!r}, expected 'tts'")
        require_usable_tts_voice(f"tts_config_by_language[{lang!r}]", config_id, row["engine"], row["voice"])
        if supported and lang not in _row_tts_languages(row):
            raise ValueError(
                f"{language_name(lang)} ({lang}) can't be spoken by the voice override "
                f"{row['name']!r} ({row['engine']}) — choose a voice that supports {language_name(lang)}"
            )
        # Canonical (lowercase) form, so id comparisons elsewhere can't miss on case.
        overrides[lang] = str(uuid.UUID(str(config_id)))

    greetings_raw = merged.get("greeting_by_language") or {}
    if not isinstance(greetings_raw, dict):
        raise ValueError("greeting_by_language must be an object of {language: greeting}")
    greetings: dict[str, str] = {}
    for lang_raw, text in greetings_raw.items():
        lang = normalize_language(lang_raw)
        if lang not in LANGUAGES:
            raise ValueError(f"greeting_by_language has an unknown language {lang_raw!r}")
        if supported and lang not in supported:
            raise ValueError(f"greeting_by_language has a greeting for {lang_raw!r}, which is not in supported_languages")
        if not isinstance(text, str):
            raise ValueError(f"greeting_by_language[{lang!r}] must be text")
        if len(text) > _MAX_GREETING_CHARS:
            raise ValueError(f"greeting_by_language[{lang!r}] is longer than {_MAX_GREETING_CHARS} characters")
        if text.strip():
            greetings[lang] = text.strip()

    out: dict[str, Any] = {
        "supported_languages": list(supported) or None,
        "tts_config_by_language": json.dumps(overrides) if overrides else None,
        "greeting_by_language": json.dumps(greetings) if greetings else None,
    }
    if not supported:
        return out
    out["language"] = default

    # One read of both tenant defaults for the two checks below. Agent saves take FOR SHARE so a
    # concurrent update_tenant can't swap a default under them; revalidation reads unlocked
    # because its callers already serialize with update_tenant on the provider row, and
    # locking the tenant after the provider would invert update_tenant's provider -> tenant order.
    defaults = await conn.fetchrow(
        "SELECT default_stt_config_id, default_tts_config_id FROM tenants WHERE id = $1"
        + (" FOR SHARE" if lock_defaults else ""),
        tenant_id,
    )

    # Switching needs an STT that can tell the languages apart: an English-only Whisper
    # model can't detect at all, and Deepgram only code-switches on nova-2/3 'multi'.
    if len(supported) > 1:
        stt_id = merged.get("stt_config_id") or defaults["default_stt_config_id"]
        stt = await _tts_row(conn, str(stt_id), tenant_id) if stt_id else None
        if stt is not None and stt["role"] == "stt":
            name = f"{stt['name']!r} ({stt['engine']} {stt['model'] or 'default model'})"
            if stt["engine"] == "faster_whisper" and (stt["model"] or "").endswith(".en"):
                raise ValueError(
                    f"the agent's speech recognition {name} is English-only and can't detect other "
                    "languages — use a multilingual Whisper model (e.g. small)"
                )
            if stt["engine"] == "deepgram" and not deepgram_supports_multi(stt["model"], list(supported)):
                raise ValueError(
                    f"the agent's speech recognition {name} can't switch between "
                    f"{', '.join(language_name(l) for l in supported)} — use nova-3 or nova-2 "
                    f"('{DEEPGRAM_MULTI}' covers en, es, fr, de, hi, ru, pt, ja, it, nl)"
                )

    # Every non-English language needs a voice that can speak it: a live caller must
    # never hear Hindi read by an English-only engine.
    base_id = merged.get("tts_config_id") or defaults["default_tts_config_id"]
    base = await _tts_row(conn, str(base_id), tenant_id) if base_id else None
    base_langs = _row_tts_languages(base) if base is not None else frozenset({"en"})
    for lang in supported:
        if lang == "en" or lang in overrides or lang in base_langs:
            continue
        provider = f"{base['name']!r} ({base['engine']})" if base is not None else "(none configured)"
        raise ValueError(
            f"{language_name(lang)} ({lang}) can't be spoken by the agent's TTS provider {provider} — "
            f"choose a multilingual TTS provider or add a {language_name(lang)} voice override"
        )
    return out


def _row_tts_languages(row: Any) -> frozenset[str]:
    return tts_languages(
        row["engine"], tts_model_of(row["engine"], row["model"], db.json_col(row["extra"]) or {}), row["voice"],
    )


async def revalidate_multilingual_agents(
    conn: Any, tenant_id: Any, *, provider_id: Any = None, default_roles: tuple[str, ...] = (),
) -> None:
    """Re-run the multilingual checks, inside the caller's transaction (after its write), for
    the multilingual agents that use what was just edited: the provider row `provider_id`
    (as STT, TTS, a voice override, or the tenant default an agent falls back to), or the
    tenant defaults in `default_roles` ("stt"/"tts") for agents without their own.

    Provider and tenant-default edits change what an agent's voices and STT can do without
    the agent being saved, e.g. a Kokoro override's voice from hf_alpha to af_heart. Agents
    that don't use the edited row are left alone, so an unrelated edit is never blocked.
    Raises ValueError naming the agent it would break."""
    rows = await conn.fetch(
        "SELECT name, language, supported_languages, tts_config_by_language, greeting_by_language, "
        "tts_config_id, stt_config_id FROM agents "
        "WHERE tenant_id = $1 AND deleted_at IS NULL AND cardinality(supported_languages) > 0",
        tenant_id,
    )
    def canonical(value: Any) -> str | None:
        """Lowercase UUID string, so a differently-cased id still matches."""
        if value is None:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except ValueError:
            return str(value)

    pid = canonical(provider_id)
    defaults: dict[str, str | None] = {}
    for role in ("stt", "tts"):
        value = await conn.fetchval(f"SELECT default_{role}_config_id FROM tenants WHERE id = $1", tenant_id)
        defaults[role] = canonical(value)

    def uses_edit(row: Any) -> bool:
        own = {role: canonical(row[f"{role}_config_id"]) for role in ("stt", "tts")}
        if any(own[role] is None for role in default_roles):
            return True
        if pid is None:
            return False
        effective = {role: own[role] or defaults[role] for role in ("stt", "tts")}
        overrides = db.json_col(row["tts_config_by_language"]) or {}
        return pid in effective.values() or pid in {canonical(v) for v in overrides.values()}

    for row in rows:
        if not uses_edit(row):
            continue
        merged = {f: row[f] for f in _LANGUAGE_FIELDS}
        for col in ("tts_config_by_language", "greeting_by_language"):
            merged[col] = db.json_col(merged[col])
        for col in ("tts_config_id", "stt_config_id"):
            merged[col] = str(merged[col]) if merged[col] is not None else None
        try:
            await _validate_languages(conn, tenant_id, merged)
        except ValueError as exc:
            raise ValueError(f"this change would break multilingual agent {row['name']!r}: {exc}") from None


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
    language: str | None = None,
    supported_languages: list[str] | None = None,
    tts_config_by_language: dict[str, str] | None = None,
    greeting_by_language: dict[str, str] | None = None,
    status: str = "active",
    template_id: str | None = None,
    template_version: int | None = None,
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
    async with tenant_conn(pool) as conn:
        await _validate_provider_assignments(
            conn, tenant_id,
            {"stt_config_id": stt_config_id, "llm_config_id": llm_config_id, "tts_config_id": tts_config_id},
        )
        _check_language_tag(language)
        language = language.strip() if isinstance(language, str) else language
        langs = await _validate_languages(conn, tenant_id, {
            "stt_config_id": stt_config_id,
            "language": language, "supported_languages": supported_languages,
            "tts_config_by_language": tts_config_by_language,
            "greeting_by_language": greeting_by_language, "tts_config_id": tts_config_id,
        }, lock_defaults=True)
        row = await conn.fetchrow(
            "INSERT INTO agents "
            "(tenant_id, slug, name, greeting, system_prompt, "
            "stt_config_id, llm_config_id, tts_config_id, workflow, workflow_draft, "
            "language, status, template_id, template_version, "
            "supported_languages, tts_config_by_language, greeting_by_language) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $9::jsonb, $10, $11, $12, $13, "
            "$14, $15::jsonb, $16::jsonb) "
            "RETURNING *",
            tenant_id, slug, name, greeting, system_prompt,
            stt_config_id, llm_config_id, tts_config_id, graph_json,
            langs.get("language", language), status, template_id, template_version,
            langs["supported_languages"], langs["tts_config_by_language"],
            langs["greeting_by_language"],
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
        # Warm the cache now rather than on the agent's first live call.
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
    """tenant_slug scopes the lookup and names the cache key to invalidate."""
    if not fields:
        raise ValueError("update_agent() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_agent() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # Row lock keeps the audit "old" value accurate; the tenant join makes
        # another tenant's agent_id indistinguishable from not-found.
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
        if "language" in fields:
            _check_language_tag(fields["language"])
            if isinstance(fields["language"], str):
                fields = {**fields, "language": fields["language"].strip()}
                set_fields["language"] = fields["language"]
        if any(f in fields for f in _LANGUAGE_FIELDS):
            merged = {f: fields.get(f, old.get(f)) for f in _LANGUAGE_FIELDS}
            langs = await _validate_languages(conn, old["tenant_id"], merged, lock_defaults=True)
            # Only write the multilingual columns this request touched (plus the
            # normalised default language), so a single-language save stays byte-identical.
            for col, value in langs.items():
                if col in fields or col == "language":
                    set_fields[col] = value
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
        old_audit = _strip_slot(old) if mirrored_graph else _audit_view(old)
        new_audit = _strip_slot(new) if mirrored_graph else _audit_view(new)
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
    # A published IVR flow handing calls to this agent cached whether it can
    # answer; a deactivation must reach the next call, not the next TTL.
    await call_flows.invalidate_runtime_caches_naming_agent(old["tenant_id"], tenant_slug, agent_id)
    return _public_agent(new)


async def _replace_prompt(
    agent_id: Any,
    *,
    tenant_id: Any,
    tenant_slug: str,
    user_id: Any | None,
    user_email: str | None,
    new_prompt: Callable[[dict[str, Any]], str | None],
    update_sql: str,
    update_args: tuple[Any, ...],
    note: str,
) -> dict[str, Any] | None:
    """Shared body of Accept and Undo: lock the row, mirror the new prompt into
    the graphs, run the one conditional update that decides the outcome, then
    version, audit and invalidate. `update_sql` takes $1 id, $2 tenant, then
    `update_args`, then the two mirrored graphs as the last two parameters.
    new_prompt maps the locked row to the prompt being written, since Undo
    reads it from the slot."""
    from .workflows import append_version

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM agents WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL FOR UPDATE",
            agent_id, tenant_id,
        )
        if old_row is None:
            return None
        old = _row(old_row)
        fields = {"system_prompt": new_prompt(old)}
        graph = _mirror_prompts_into_graph(old["workflow"], fields)
        draft = _mirror_prompts_into_graph(old["workflow_draft"], fields)
        graph_json = None if graph is None else json.dumps(graph)
        draft_json = None if draft is None else json.dumps(draft)

        new_row = await conn.fetchrow(update_sql, agent_id, tenant_id, *update_args, graph_json, draft_json)
        if new_row is None:
            return None
        new = _row(new_row)

        if graph_json is not None:
            await append_version(conn, agent_id, graph_json, user_id=user_id, note=note)
        await audit.write_audit(
            conn,
            entity_type="agent",
            entity_id=agent_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value=_strip_slot(old),
            new_value=_strip_slot(new),
        )

    await cache.invalidate(cache_key(tenant_slug, old["slug"]))
    await call_flows.invalidate_runtime_caches_naming_agent(tenant_id, tenant_slug, agent_id)
    return _public_agent(new)


async def accept_prompt_revision(
    agent_id: Any,
    *,
    tenant_id: Any,
    tenant_slug: str,
    proposed_prompt: str,
    base_prompt_sha256: str,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any] | None:
    """Swap in a revised prompt only if the stored prompt still hashes to
    base_prompt_sha256, keeping the old one in the undo slot. None means the
    conditional update matched no row (stale base, or no such agent)."""
    return await _replace_prompt(
        agent_id, tenant_id=tenant_id, tenant_slug=tenant_slug, user_id=user_id, user_email=user_email,
        new_prompt=lambda _old: proposed_prompt,
        update_sql=(
            "UPDATE agents SET system_prompt = $4, prompt_undo_previous = system_prompt, "
            "prompt_undo_accepted_sha256 = $5, workflow = $6::jsonb, workflow_draft = $7::jsonb, "
            "updated_at = now() "
            "WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL "
            f"AND {_PROMPT_SHA_SQL} = $3 RETURNING *"
        ),
        update_args=(base_prompt_sha256, proposed_prompt, _sha256_hex(proposed_prompt)),
        note="prompt revision accepted",
    )


async def undo_prompt_revision(
    agent_id: Any,
    *,
    tenant_id: Any,
    tenant_slug: str,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any] | None:
    """Restore the prompt from before the last accepted fix, only while the
    stored prompt is still the one that fix wrote. None means nothing to undo."""
    return await _replace_prompt(
        agent_id, tenant_id=tenant_id, tenant_slug=tenant_slug, user_id=user_id, user_email=user_email,
        new_prompt=lambda old: old["prompt_undo_previous"],
        update_sql=(
            "UPDATE agents SET system_prompt = prompt_undo_previous, prompt_undo_previous = NULL, "
            "prompt_undo_accepted_sha256 = NULL, workflow = $3::jsonb, workflow_draft = $4::jsonb, "
            "updated_at = now() "
            "WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL AND prompt_undo_previous IS NOT NULL "
            f"AND {_PROMPT_SHA_SQL} = prompt_undo_accepted_sha256 RETURNING *"
        ),
        update_args=(),
        note="prompt revision undone",
    )


class AgentHasLiveCalls(Exception):
    """Refuses deleting an agent with a call in progress; deliberately no force override."""

    def __init__(self, live_call_count: int) -> None:
        self.live_call_count = live_call_count
        super().__init__(f"{live_call_count} call(s) in progress on this agent — wait for them to end")


async def soft_delete_agent(
    agent_id: Any,
    *,
    tenant_slug: str,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # See update_agent()'s comment — same tenant-scoping + row-lock fix.
        old_row = await conn.fetchrow(
            "SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
            "WHERE a.id = $1 AND t.slug = $2 FOR UPDATE OF a",
            agent_id, tenant_slug,
        )
        if old_row is None:
            raise LookupError(f"agent {agent_id} not found under tenant {tenant_slug!r}")
        old = _row(old_row)

        live_calls = await conn.fetchval(
            "SELECT count(*) FROM calls WHERE agent_id = $1 AND ended_at IS NULL", agent_id,
        )
        if live_calls:
            raise AgentHasLiveCalls(live_calls)

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
    await call_flows.invalidate_runtime_caches_naming_agent(old["tenant_id"], tenant_slug, agent_id)
