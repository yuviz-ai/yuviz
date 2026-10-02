"""Telephony config CRUD with cache-aside reads and audited mutations.

`credentials` is validated against the provider registry and returned unredacted
(sensitive fields sealed); router-level auth is the only gate.
"""

from __future__ import annotations

import json as _json
from typing import Any

import asyncpg

from libs.config_sdk.secrets import encrypt_secret, is_encrypted
from libs.telephony_sdk.exceptions import TelephonyProviderError
from libs.telephony_sdk import providers as _providers  # noqa: F401 — registers every built-in provider
from libs.telephony_sdk.registry import TelephonyProviderRegistry
from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, db

_UPDATABLE_FIELDS = {"name", "credentials", "is_default_outbound"}
_PLATFORM_MANAGED_CREDENTIAL_FIELDS = ("inbound_application_id",)

# 'native' is the shared Kamailio/FreeSWITCH: no ITelephonyProvider, no credentials, no health.
NATIVE_PROVIDER = "native"
_NON_REST_PROVIDERS = frozenset({NATIVE_PROVIDER})


class TelephonyConfigHasNumbers(Exception):
    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"{count} number(s) still use this configuration; remove or move them first, "
            "so none is left routing calls to a configuration that no longer exists"
        )


class NativeConfigExists(Exception):
    def __init__(self) -> None:
        super().__init__("this account already has a Native (local SIP) configuration")


def _cache_key(config_id: Any) -> str:
    return f"telephony_config:{config_id}"


def _normalize_scalar_or_list(field_name: str, value: Any) -> Any:
    """Encrypts plaintext, keeps `enc:` tokens, rejects `env:`/`k8s:` refs (tenant input
    must not read platform secrets). Handles scalar and list-valued fields."""
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
    """Seals the provider's sensitive fields; non-REST providers are returned verbatim."""
    if provider in _NON_REST_PROVIDERS:
        return credentials
    provider_cls = TelephonyProviderRegistry.get(provider)
    sealed = dict(credentials)
    for field_name in provider_cls.sensitive_credential_fields():
        if field_name in sealed:
            sealed[field_name] = _normalize_scalar_or_list(field_name, sealed[field_name])
    return sealed


def validate_credentials(provider: str, credentials: dict[str, Any]) -> None:
    """Raises ValueError (mapped to 400 by app.py). Native configs skip validation."""
    if provider in _NON_REST_PROVIDERS:
        return
    try:
        provider_cls = TelephonyProviderRegistry.get(provider)
        provider_cls.validate_credentials(credentials)
    except (ValueError, TelephonyProviderError) as exc:
        raise ValueError(str(exc)) from exc


def list_supported_providers() -> dict[str, dict[str, list[str]]]:
    """Provider name -> {required, sensitive} credential fields. Native is added explicitly:
    it must not be registered, since Campaigns treats registered providers as REST-dialable."""
    supported = {
        name: {
            "required": provider_cls.required_credential_fields(),
            "sensitive": provider_cls.sensitive_credential_fields(),
        }
        for name, provider_cls in TelephonyProviderRegistry.visible().items()
    }
    supported[NATIVE_PROVIDER] = {"required": [], "sensitive": []}
    return supported


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


async def get_provider_kind(config_id: Any) -> str | None:
    """The config's provider even after a soft delete; None only if no such row."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="telephony-config-kind") as conn:
        return await conn.fetchval("SELECT provider FROM telephony_configs WHERE id = $1", config_id)


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
        # Kept out of the cached row so the 60s TTL can't freeze a stale badge.
        health = await cache.get_json(f"telephony:health:{result['id']}")
        result["health"] = health or {"status": "standby", "checked_at": None}
    return results


async def list_configs_by_provider(provider: str) -> list[dict[str, Any]]:
    """Cross-tenant by design: the telephony service's account preload needs every tenant's rows."""
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

        try:
            row = await conn.fetchrow(
                "INSERT INTO telephony_configs "
                "(tenant_id, name, provider, credentials, is_default_outbound) "
                "VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING *",
                tenant_id, name, provider, _json.dumps(credentials), is_default_outbound,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "telephony_configs_one_native_per_tenant":
                raise NativeConfigExists() from None
            raise
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


def _account_identity(provider: str, credentials: dict[str, Any]) -> tuple:
    """The non-secret required fields name the provider account (Vobiz
    auth_id, Cloudonix domain)."""
    if provider not in TelephonyProviderRegistry.all():
        return ()
    cls = TelephonyProviderRegistry.get(provider)
    secret = set(cls.sensitive_credential_fields())
    return tuple(credentials.get(f) for f in cls.required_credential_fields() if f not in secret)


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
            # Ids the platform created at the provider (number_sync.py) aren't
            # in the admin's form; dropping them would orphan that resource.
            # Only within the same provider account: another account can't use them.
            old_credentials = db.json_col(old["credentials"]) or {}
            if _account_identity(old["provider"], old_credentials) == _account_identity(old["provider"], new_credentials):
                for key in _PLATFORM_MANAGED_CREDENTIAL_FIELDS:
                    if key in old_credentials and key not in new_credentials:
                        new_credentials[key] = old_credentials[key]
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

        # Only written columns; "[redacted]" is the marker the UI shows as a changed secret.
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
    """Atomically makes config_id the tenant's only default-outbound config."""
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
        # audit_log.action is CHECK-constrained to created/updated/deleted.
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
        numbers = await conn.fetchval(
            "SELECT count(*) FROM phone_numbers WHERE telephony_config_id = $1 AND deleted_at IS NULL", config_id,
        )
        if numbers:
            raise TelephonyConfigHasNumbers(numbers)

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
