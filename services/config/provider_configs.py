"""Provider config CRUD with cache-aside reads and audited mutations.

api_key_ref is returned as stored (a pointer or sealed `enc:` value, never a resolved secret).
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


# ponytail: enc: refs are returned unmasked; Conversation Service needs the sealed value on a Redis miss.
def resolve_api_key_input(api_key: str | None, api_key_ref: str | None) -> str | None:
    """Encrypts a typed key, stores a pointer verbatim, rejects a raw key in the pointer field.
    None means no credential (NULL column)."""
    ref = (api_key_ref or "").strip()
    if api_key and ref:
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

# Conversation Service evicts its cached provider client on this channel.
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
    # `api_key` folds into api_key_ref. Absent means untouched; present-but-empty clears it.
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
        # FOR UPDATE so a concurrent update can't make the audit old_value stale.
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

        # Only written columns, so a redacted api_key_ref doesn't show as changed on every update.
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


# Other TTS engines fall back to a default voice; ElevenLabs fails at call time.
_TTS_ENGINES_REQUIRING_VOICE = {"elevenlabs"}


def require_usable_tts_voice(field: str, config_id: Any, engine: str, voice: str | None) -> None:
    """One rule for every TTS assignment (agent or account default): an
    engine that needs a voice fails at call time without one."""
    if engine in _TTS_ENGINES_REQUIRING_VOICE and not voice:
        raise ValueError(
            f"{field}={config_id!r} is a {engine} provider with no voice selected — "
            "pick a voice for it before assigning it"
        )


class ProviderConfigInUse(Exception):
    """Raised instead of deleting while an agent, knowledge base, or tenant default still uses the provider."""

    def __init__(self, resource_type: str, resource_count: int, resource_names: list[str]) -> None:
        self.resource_type = resource_type
        self.resource_count = resource_count
        self.resource_names = resource_names
        if resource_type == "tenant_default":
            message = "this provider is the account's default; choose another default before deleting it"
        else:
            noun = "agent" if resource_type == "agent" else "knowledge base"
            message = f"{resource_count} {noun}(s) use this provider — reassign them before deleting it"
        super().__init__(message)


# role -> (table, tenant column, column referencing the provider, resource_type).
_ROLE_TO_USAGE_CHECK = {
    "stt":       ("agents", "tenant_id", "stt_config_id", "agent"),
    "llm":       ("agents", "tenant_id", "llm_config_id", "agent"),
    "tts":       ("agents", "tenant_id", "tts_config_id", "agent"),
    "embedding": ("knowledge_bases", "tenant_id", "embedding_config_id", "knowledge_base"),
}


async def soft_delete_provider_config(
    provider_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    """Refuses while it is the account default or any non-deleted agent/KB references it.
    No force option: dependents would silently fall back to the default script."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM provider_configs WHERE id = $1 FOR UPDATE", provider_id,
        )
        if old_row is None:
            raise LookupError(f"provider_config {provider_id} not found")
        old = dict(old_row)
        old["extra"] = db.json_col(old["extra"])

        # Agents with no provider of their own fall back to the account default.
        if old["role"] in ("stt", "llm", "tts"):
            tenant_name = await conn.fetchval(
                f"SELECT name FROM tenants WHERE id = $1 AND default_{old['role']}_config_id = $2",
                old["tenant_id"], provider_id,
            )
            if tenant_name is not None:
                raise ProviderConfigInUse("tenant_default", 1, [tenant_name])

        check = _ROLE_TO_USAGE_CHECK.get(old["role"])
        if check is not None:
            table, tenant_column, ref_column, resource_type = check
            rows = await conn.fetch(
                f"SELECT name FROM {table} WHERE {tenant_column} = $1 AND deleted_at IS NULL "
                f"AND {ref_column} = $2",
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
    """Lists ElevenLabs voices server-side; the resolved key is never returned to the caller."""
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
        raise ValueError(f"could not reach the ElevenLabs Voices API: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        # Log the upstream body but don't forward it in the 400 detail.
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
            # Validated ISO 639-1 languages; often empty, so callers fall back to free-form `labels`.
            "verified_languages": v.get("verified_languages") or [],
        }
        for v in resp.json().get("voices", [])
    ]
