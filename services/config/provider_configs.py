"""
Provider config CRUD — same cache-aside + audited-mutation pattern.

Returning api_key_ref is fine for the pointer schemes: it's a path, never a
resolved secret. An `enc:` ref CARRIES the credential instead, and is still
returned — Conversation Service reads this endpoint on a Redis miss and
needs the sealed value. See the ponytail note on resolve_api_key_input().
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from libs.config_sdk.secrets import ENCRYPTED_PREFIX, encrypt_secret
from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, db
from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

_SECRET_SCHEMES = ("env:", "k8s:", ENCRYPTED_PREFIX)


# ponytail: an enc: ref is returned unmasked. Fernet ciphertext is worthless
# without SECRET_ENCRYPTION_KEY, and masking here would break Conversation
# Service, which reads this endpoint on a Redis miss. Mask server-side once
# a consumer exists that is neither a browser nor that service.
def resolve_api_key_input(api_key: str | None, api_key_ref: str | None) -> str | None:
    """Turns what the UI sent into what belongs in the column: a typed key is
    encrypted, a pointer is stored verbatim, and a raw key pasted into the
    pointer field is rejected — that mistake stored a live Gemini key in
    plaintext. None means no credential, i.e. a NULL column."""
    ref = (api_key_ref or "").strip()
    if api_key and ref:
        # Ambiguous, not a legitimate double-write — the real "replace this
        # ref with a typed key" case sends api_key_ref="" alongside api_key
        # (see secretPayload() in the Admin UI), so ref is blank there.
        # Both non-blank at once means something upstream picked the wrong
        # field, not a real caller intent.
        raise ValueError("send either api_key or api_key_ref, not both")
    if api_key:
        return encrypt_secret(api_key.strip())
    if not ref:
        return None
    if ref.startswith(_SECRET_SCHEMES):
        return ref
    raise ValueError(
        "api_key_ref must point at a secret (env:VAR_NAME or "
        "k8s:namespace/secret). To store the key itself, send it as `api_key` "
        "and it will be encrypted — never paste a real key into this field."
    )

_UPDATABLE_FIELDS = {
    "name", "engine", "model", "voice", "language", "region", "environment",
    "api_key_ref", "extra",
}

# Conversation Service subscribes to this channel (see
# services/conversation/provider_config_subscriber.py) so an edit here
# evicts the corresponding cached provider client instantly, instead of
# needing a full process restart.
PROVIDER_CONFIG_CHANGED_CHANNEL = "provider_config_changed"


def _cache_key(provider_id: Any) -> str:
    return f"provider:{provider_id}"


async def get_provider_config(provider_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    cached = await cache.get_json(_cache_key(provider_id))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="provider-configs-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM provider_configs WHERE id = $1 AND deleted_at IS NULL", provider_id,
        )
    if row is None:
        return None

    result = dict(row)
    result["extra"] = db.json_col(result["extra"])
    await cache.set_json(_cache_key(provider_id), result)
    return result


async def list_provider_configs(
    tenant_id: Any, *, role: str | None = None, environment: str | None = None,
) -> list[dict[str, Any]]:
    """Used to populate the Admin UI's STT/LLM/TTS dropdowns — prod-first,
    dev-last ordering is a presentation concern for the caller, not baked in
    here."""
    pool = await db.get_pool()
    conditions = ["tenant_id = $1", "deleted_at IS NULL"]
    params: list[Any] = [tenant_id]
    if role is not None:
        params.append(role)
        conditions.append(f"role = ${len(params)}")
    if environment is not None:
        params.append(environment)
        conditions.append(f"environment = ${len(params)}")

    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            f"SELECT * FROM provider_configs WHERE {' AND '.join(conditions)} ORDER BY name",
            *params,
        )
    results = [dict(row) for row in rows]
    for result in results:
        result["extra"] = db.json_col(result["extra"])
    return results


async def create_provider_config(
    *,
    tenant_id: Any,
    name: str,
    role: str,
    engine: str,
    environment: str = "prod",
    model: str | None = None,
    voice: str | None = None,
    language: str | None = None,
    region: str | None = None,
    api_key_ref: str | None = None,
    api_key: str | None = None,
    extra: dict[str, Any] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    import json as _json

    api_key_ref = resolve_api_key_input(api_key, api_key_ref)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO provider_configs "
            "(tenant_id, name, role, engine, environment, model, voice, language, region, api_key_ref, extra) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb) RETURNING *",
            tenant_id, name, role, engine, environment,
            model, voice, language, region, api_key_ref,
            _json.dumps(extra) if extra is not None else None,
        )
        result = dict(row)
        result["extra"] = db.json_col(result["extra"])
        await audit.write_audit(
            conn,
            entity_type="provider_config",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value=result,
        )
    return result


async def update_provider_config(
    provider_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    # `api_key` is a credential, not a column — popped before the
    # unknown-field check, encrypted, and folded into api_key_ref. Absent
    # from `fields` means untouched; present-but-empty is an explicit clear.
    had_api_key = "api_key" in fields
    typed_key = fields.pop("api_key", None)
    if had_api_key or "api_key_ref" in fields:
        fields["api_key_ref"] = resolve_api_key_input(typed_key, fields.get("api_key_ref"))

    if not fields:
        raise ValueError("update_provider_config() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_provider_config() got non-updatable field(s): {unknown}")

    import json as _json
    if "extra" in fields and fields["extra"] is not None:
        fields = {**fields, "extra": _json.dumps(fields["extra"])}

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # FOR UPDATE — see tenants.py's update_tenant() comment: without
        # it, a concurrent update could make this transaction's audit
        # entry record a stale old_value.
        old_row = await conn.fetchrow(
            "SELECT * FROM provider_configs WHERE id = $1 FOR UPDATE", provider_id,
        )
        if old_row is None:
            raise LookupError(f"provider_config {provider_id} not found")
        old = dict(old_row)
        old["extra"] = db.json_col(old["extra"])

        columns = list(fields.keys())
        set_parts = []
        for i, col in enumerate(columns):
            cast = "::jsonb" if col == "extra" else ""
            set_parts.append(f"{col} = ${i + 2}{cast}")
        new_row = await conn.fetchrow(
            f"UPDATE provider_configs SET {', '.join(set_parts)}, updated_at = now() "
            f"WHERE id = $1 RETURNING *",
            provider_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)
        new["extra"] = db.json_col(new["extra"])

        # Scoped to the written columns, not the full row — otherwise
        # api_key_ref (redacted either way) rides along on every update
        # and the UI can't tell "redacted, unchanged" from "redacted,
        # changed."
        await audit.write_audit(
            conn,
            entity_type="provider_config",
            entity_id=provider_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value={col: old[col] for col in columns},
            new_value={col: new[col] for col in columns},
        )

    await cache.invalidate(_cache_key(provider_id))
    await cache.publish(PROVIDER_CONFIG_CHANGED_CHANNEL, str(provider_id))
    return new


class ProviderConfigInUse(Exception):
    """Raised instead of deleting when active agents (stt/llm/tts roles) or
    active knowledge bases (embedding role) still have this provider
    assigned and the caller didn't pass force=True — whichever one points
    at a deleted provider fails to resolve it the next time it needs it
    (an agent mid-call-setup; a knowledge base mid-ingest or mid-retrieval,
    see services/knowledge/{retrieval,ingestion_worker}.py's own
    _fetch_embedding_config, which raises outright on a missing row).
    Resource names, not just a count, so the admin sees exactly who's
    affected without a second lookup. `resource_type` tells the caller
    which noun to use in copy — "agent" or "knowledge_base" — since the
    same shape covers both dependency kinds."""

    def __init__(self, resource_type: str, resource_count: int, resource_names: list[str]) -> None:
        self.resource_type = resource_type
        self.resource_count = resource_count
        self.resource_names = resource_names
        noun = "agent" if resource_type == "agent" else "knowledge base"
        super().__init__(
            f"{resource_count} active {noun}(s) use this provider — pass force=True to delete anyway"
        )


# role -> (table to check, its tenant-scope FK column, the column pointing
# at this provider, resource_type label). stt/llm/tts are referenced by
# agents directly; embedding is referenced by knowledge_bases instead (see
# database/knowledge_schema.sql's embedding_config_id) — agents never point
# at an embedding provider directly, so checking agents for that role would
# silently miss the real dependency.
_ROLE_TO_USAGE_CHECK = {
    "stt":       ("agents", "tenant_id", "stt_config_id", "agent"),
    "llm":       ("agents", "tenant_id", "llm_config_id", "agent"),
    "tts":       ("agents", "tenant_id", "tts_config_id", "agent"),
    "embedding": ("knowledge_bases", "tenant_id", "embedding_config_id", "knowledge_base"),
}


async def soft_delete_provider_config(
    provider_id: Any, *, user_id: Any | None = None, user_email: str | None = None, force: bool = False,
) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM provider_configs WHERE id = $1 FOR UPDATE", provider_id,
        )
        if old_row is None:
            raise LookupError(f"provider_config {provider_id} not found")
        old = dict(old_row)
        old["extra"] = db.json_col(old["extra"])

        if not force:
            check = _ROLE_TO_USAGE_CHECK.get(old["role"])
            if check is not None:
                table, tenant_column, ref_column, resource_type = check
                rows = await conn.fetch(
                    f"SELECT name FROM {table} WHERE {tenant_column} = $1 AND deleted_at IS NULL "
                    f"AND status = 'active' AND {ref_column} = $2",
                    old["tenant_id"], provider_id,
                )
                if rows:
                    raise ProviderConfigInUse(resource_type, len(rows), [r["name"] for r in rows])

        await conn.execute(
            "UPDATE provider_configs SET deleted_at = now() WHERE id = $1", provider_id,
        )
        await audit.write_audit(
            conn,
            entity_type="provider_config",
            entity_id=provider_id,
            action="deleted",
            user_id=user_id,
            user_email=user_email,
            old_value=old,
        )

    await cache.invalidate(_cache_key(provider_id))
    await cache.publish(PROVIDER_CONFIG_CHANGED_CHANNEL, str(provider_id))


_ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"


async def list_elevenlabs_voices(provider_id: Any, *, secret_resolver: SecretResolver) -> list[dict[str, Any]]:
    """Calls ElevenLabs' own Voices API server-side using provider_id's
    api_key_ref — the resolved key is used for this one outbound call and
    never returned to the caller (see secret_resolver.py's module
    docstring: this is the one place Config Service resolves a secret,
    specifically so the admin-ui never has to)."""
    cfg = await get_provider_config(provider_id)
    if cfg is None:
        raise LookupError(f"provider_config {provider_id} not found")
    if cfg["role"] != "tts" or cfg["engine"] != "elevenlabs":
        raise ValueError(
            f"provider_config {provider_id} is role={cfg['role']!r} engine={cfg['engine']!r}, "
            "expected role='tts' engine='elevenlabs'"
        )
    if not cfg["api_key_ref"]:
        raise ValueError(f"provider_config {provider_id} has no api_key_ref configured")

    api_key = await secret_resolver.resolve(cfg["api_key_ref"])
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_ELEVENLABS_VOICES_URL, headers={"xi-api-key": api_key})
    except httpx.RequestError as exc:
        # DNS failure, connect timeout, read timeout — nothing else catches
        # this (the router doesn't either), so left unhandled it reaches the
        # admin-ui as a bare 500 with no actionable detail.
        raise ValueError(f"could not reach the ElevenLabs Voices API: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        # Log the real response body (useful for debugging a bad key/rate
        # limit) but never forward it in the ValueError: the router maps
        # ValueError to a 400 `detail`, and the ElevenLabs response body is
        # not ours to hand back to the admin-ui caller verbatim.
        log.warning(
            "ElevenLabs Voices API returned %s for provider_config %s: %s",
            resp.status_code, provider_id, resp.text[:200],
        )
        raise ValueError(f"ElevenLabs Voices API returned {resp.status_code}")

    return [
        {
            "voice_id": v["voice_id"],
            "name": v["name"],
            "category": v.get("category"),
            "labels": v.get("labels") or {},
            "preview_url": v.get("preview_url"),
            # ElevenLabs documents `labels` as arbitrary, unvalidated
            # metadata (any string a voice's owner chose to tag it with) —
            # `verified_languages` is the actual validated field: each entry
            # is a real ISO 639-1 code this voice has been confirmed to
            # speak, with its own model_id/accent/locale/preview_url. Not
            # every voice has this populated (e.g. voices never run through
            # ElevenLabs' verification), so callers should fall back to
            # `labels` when it's empty, not assume it's always present.
            "verified_languages": v.get("verified_languages") or [],
        }
        for v in resp.json().get("voices", [])
    ]
