"""
Telephony config CRUD — same cache-aside + audited-mutation pattern as
provider_configs.py. `credentials` is validated against the registered
provider (libs.telephony_sdk.registry.TelephonyProviderRegistry) before
insert, so a malformed/incomplete credential set never reaches Postgres.

Returning `credentials` to a caller is intentionally NOT redacted here —
unlike provider_configs.api_key_ref (a reference path, never a secret
itself), telephony credentials ARE the actual secret (auth_id/auth_token).
Callers of get/list must be trusted admin/superadmin routes only (see
routers/telephony_configs.py's require_role guards) — this mirrors how
phone_numbers/provider_configs already assume router-level auth is the
only gate, not a second field-level redaction layer.
"""

from __future__ import annotations

import json as _json
from typing import Any

from libs.config_sdk.secrets import encrypt_secret, is_encrypted
from libs.telephony_sdk.exceptions import TelephonyProviderError
from libs.telephony_sdk import providers as _providers  # noqa: F401 — registers every built-in provider
from libs.telephony_sdk.registry import TelephonyProviderRegistry
from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, db

_UPDATABLE_FIELDS = {"name", "credentials", "is_default_outbound"}

# 'native' is the 5000-5009 Kamailio/FreeSWITCH rows after the relabel
# migration (T23) — the REST plane never serves them, so there is no
# ITelephonyProvider to validate/normalize credentials against and no
# health to probe. Every other registered provider name is REST-capable.
_NON_REST_PROVIDERS = frozenset({"native"})


def _cache_key(config_id: Any) -> str:
    return f"telephony_config:{config_id}"


def _normalize_scalar_or_list(field_name: str, value: Any) -> Any:
    """Seals a scalar sensitive field (Vobiz's auth_token) or every entry
    of a list-valued one (Cloudonix's api_keys): a plaintext value is
    encrypted, an existing `enc:` token is kept verbatim, and anything
    else — including `env:`/`k8s:` — is rejected. `telephony_configs.
    credentials` is tenant-writable JSONB, and `CompositeSecretResolver`'s
    `env:`/`k8s:` schemes were built for admin-entered infra config, not
    tenant input (lesson 37)."""
    if isinstance(value, list):
        return [_normalize_one(field_name, entry) for entry in value]
    return _normalize_one(field_name, value)


def _normalize_one(field_name: str, entry: Any) -> str:
    if not isinstance(entry, str) or not entry:
        raise ValueError(f"{field_name} entries must be non-empty strings")
    if is_encrypted(entry):
        return entry
    if entry.startswith(("env:", "k8s:")):
        raise ValueError(f"{field_name} must be the credential itself, not an env:/k8s: reference")
    return encrypt_secret(entry)


def _normalize_credentials(provider: str, credentials: dict[str, Any]) -> dict[str, Any]:
    """Provider-agnostic over `sensitive_credential_fields()` — scalar and
    list-valued fields both seal the same way. `provider in
    _NON_REST_PROVIDERS` (native's 5000-5009 rows) is returned verbatim:
    there is no ITelephonyProvider to consult, and no credential to seal."""
    if provider in _NON_REST_PROVIDERS:
        return credentials
    provider_cls = TelephonyProviderRegistry.get(provider)
    sealed = dict(credentials)
    for field_name in provider_cls.sensitive_credential_fields():
        if field_name in sealed:
            sealed[field_name] = _normalize_scalar_or_list(field_name, sealed[field_name])
    return sealed


def validate_credentials(provider: str, credentials: dict[str, Any]) -> None:
    """Raises ValueError (not TelephonyProviderError) so this flows through
    Config Service's existing ValueError -> 400 handler (app.py) without a
    new exception-handler registration — libs/telephony_sdk stays
    HTTP-agnostic, this is where it's adapted to REST semantics. The
    relabelled 5000-5009 rows (provider='native') skip validation
    entirely — there is no ITelephonyProvider for them, and they must stay
    editable from the Telephony page after the migration."""
    if provider in _NON_REST_PROVIDERS:
        return
    try:
        provider_cls = TelephonyProviderRegistry.get(provider)
        provider_cls.validate_credentials(credentials)
    except (ValueError, TelephonyProviderError) as exc:
        raise ValueError(str(exc)) from exc


def list_supported_providers() -> dict[str, dict[str, list[str]]]:
    """Backs the discovery endpoint ("List Supported Providers") — name ->
    {required, sensitive} credential fields, so an admin UI can render the
    right form without hardcoding per-provider fields. Built from
    TelephonyProviderRegistry.visible() so "fake" never appears (AC4)."""
    return {
        name: {
            "required": provider_cls.required_credential_fields(),
            "sensitive": provider_cls.sensitive_credential_fields(),
        }
        for name, provider_cls in TelephonyProviderRegistry.visible().items()
    }


async def get_telephony_config(config_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    cached = await cache.get_json(_cache_key(config_id))
    if cached is not None:
        return cached

    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="telephony-configs-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM telephony_configs WHERE id = $1 AND deleted_at IS NULL", config_id,
        )
    if row is None:
        return None

    result = dict(row)
    result["credentials"] = db.json_col(result["credentials"])
    await cache.set_json(_cache_key(config_id), result)
    return result


async def get_default_outbound_config(tenant_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM telephony_configs WHERE tenant_id = $1 AND is_default_outbound "
            "AND deleted_at IS NULL",
            tenant_id,
        )
    if row is None:
        return None
    result = dict(row)
    result["credentials"] = db.json_col(result["credentials"])
    return result


async def list_telephony_configs(tenant_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM telephony_configs WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name",
            tenant_id,
        )
    results = [dict(row) for row in rows]
    for result in results:
        result["credentials"] = db.json_col(result["credentials"])
        # One cache.get_json per row, deliberately NOT folded into the
        # telephony_config:{id} cached row (a 60s TTL there would freeze a
        # stale badge). A Redis outage degrades every badge to Standby,
        # matching cache.py's never-fail contract — never None here.
        health = await cache.get_json(f"telephony:health:{result['id']}")
        result["health"] = health or {"status": "standby", "checked_at": None}
    return results


async def list_configs_by_provider(provider: str) -> list[dict[str, Any]]:
    """Cross-tenant by construction — the Telephony service's cold-path
    account preload needs every tenant's rows for a given provider, not
    one tenant's. `platform_conn` is the greppable, named bypass
    (CURSOR.md) for exactly this shape of read."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="telephony-account-preload") as conn:
        rows = await conn.fetch(
            "SELECT tc.id, tc.tenant_id, t.slug AS tenant_slug, tc.provider, "
            "tc.is_default_outbound, tc.credentials "
            "FROM telephony_configs tc JOIN tenants t ON t.id = tc.tenant_id "
            "WHERE tc.provider = $1 AND tc.deleted_at IS NULL",
            provider,
        )
    results = []
    for row in rows:
        result = dict(row)
        result["credentials"] = db.json_col(result["credentials"])
        results.append(result)
    return results


async def create_telephony_config(
    *,
    tenant_id: Any,
    name: str,
    provider: str,
    credentials: dict[str, Any],
    is_default_outbound: bool = False,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    credentials = _normalize_credentials(provider, credentials)
    validate_credentials(provider, credentials)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        if is_default_outbound:
            await _clear_default_outbound(conn, tenant_id)

        row = await conn.fetchrow(
            "INSERT INTO telephony_configs "
            "(tenant_id, name, provider, credentials, is_default_outbound) "
            "VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING *",
            tenant_id, name, provider, _json.dumps(credentials), is_default_outbound,
        )
        result = dict(row)
        result["credentials"] = db.json_col(result["credentials"])
        await audit.write_audit(
            conn,
            entity_type="telephony_config",
            entity_id=result["id"],
            action="created",
            user_id=user_id,
            user_email=user_email,
            new_value={**result, "credentials": "<redacted in audit trail>"},
        )
    return result


async def update_telephony_config(
    config_id: Any,
    *,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_telephony_config() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_telephony_config() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM telephony_configs WHERE id = $1 FOR UPDATE", config_id,
        )
        if old_row is None:
            raise LookupError(f"telephony_config {config_id} not found")
        old = dict(old_row)

        if "credentials" in fields and fields["credentials"] is not None:
            new_credentials = _normalize_credentials(old["provider"], fields["credentials"])
            validate_credentials(old["provider"], new_credentials)
            fields = {**fields, "credentials": _json.dumps(new_credentials)}

        if fields.get("is_default_outbound") is True:
            await _clear_default_outbound(conn, old["tenant_id"], exclude_id=config_id)

        columns = list(fields.keys())
        set_parts = []
        for i, col in enumerate(columns):
            cast = "::jsonb" if col == "credentials" else ""
            set_parts.append(f"{col} = ${i + 2}{cast}")
        new_row = await conn.fetchrow(
            f"UPDATE telephony_configs SET {', '.join(set_parts)}, updated_at = now() "
            f"WHERE id = $1 RETURNING *",
            config_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

        # Scoped to the written columns, not the full row (same reason
        # as provider_configs.py/users.py). credentials additionally
        # gets the shared "[redacted]" marker rather than a constant
        # "<redacted in audit trail>" string — a constant compares equal
        # to itself on both sides of a genuine credentials change and
        # the UI would read that as "nothing changed," hiding it.
        old_value = {col: old[col] for col in columns}
        new_value = {col: new[col] for col in columns}
        if "credentials" in columns:
            old_value["credentials"] = "[redacted]"
            new_value["credentials"] = "[redacted]"

        await audit.write_audit(
            conn,
            entity_type="telephony_config",
            entity_id=config_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            old_value=old_value,
            new_value=new_value,
        )

    await cache.invalidate(_cache_key(config_id))
    new["credentials"] = db.json_col(new["credentials"])
    return new


async def set_default_outbound(
    config_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> dict[str, Any]:
    """Atomically makes config_id the tenant's default-outbound config,
    clearing any existing default in the same transaction — safe under the
    partial unique index on telephony_configs(tenant_id) WHERE
    is_default_outbound (mirrors Dograh's own _clear_default_outbound)."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM telephony_configs WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
            config_id,
        )
        if row is None:
            raise LookupError(f"telephony_config {config_id} not found")

        await _clear_default_outbound(conn, row["tenant_id"], exclude_id=config_id)
        new_row = await conn.fetchrow(
            "UPDATE telephony_configs SET is_default_outbound = true, updated_at = now() "
            "WHERE id = $1 RETURNING *",
            config_id,
        )
        new = dict(new_row)
        # audit_log.action is CHECK-constrained to created/updated/deleted
        # (see database/schema.sql) — "updated" is the correct bucket for
        # this action, not a custom value.
        await audit.write_audit(
            conn,
            entity_type="telephony_config",
            entity_id=config_id,
            action="updated",
            user_id=user_id,
            user_email=user_email,
            new_value={**new, "credentials": "<redacted in audit trail>"},
        )

    await cache.invalidate(_cache_key(config_id))
    new["credentials"] = db.json_col(new["credentials"])
    return new


async def _clear_default_outbound(conn: Any, tenant_id: Any, *, exclude_id: Any | None = None) -> None:
    if exclude_id is not None:
        await conn.execute(
            "UPDATE telephony_configs SET is_default_outbound = false "
            "WHERE tenant_id = $1 AND is_default_outbound AND id != $2",
            tenant_id, exclude_id,
        )
    else:
        await conn.execute(
            "UPDATE telephony_configs SET is_default_outbound = false "
            "WHERE tenant_id = $1 AND is_default_outbound",
            tenant_id,
        )


async def soft_delete_telephony_config(
    config_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM telephony_configs WHERE id = $1 FOR UPDATE", config_id,
        )
        if old_row is None:
            raise LookupError(f"telephony_config {config_id} not found")

        await conn.execute(
            "UPDATE telephony_configs SET deleted_at = now() WHERE id = $1", config_id,
        )
        await audit.write_audit(
            conn,
            entity_type="telephony_config",
            entity_id=config_id,
            action="deleted",
            user_id=user_id,
            user_email=user_email,
            old_value={**dict(old_row), "credentials": "<redacted in audit trail>"},
        )

    await cache.invalidate(_cache_key(config_id))
