"""Telephony config CRUD with cache-aside reads and audited mutations.

Sensitive credential fields hold an `enc:` ciphertext, which is a bearer
capability (lesson 43): the shared resolvers decrypt it for whichever tenant
presents it. So a client can never submit one it did not already hold
(_normalize_one compares against the row read under FOR UPDATE), and
public_telephony_config() masks every `enc:` string in a browser-facing
response. The service getters and the cache keep the sealed value for the
platform service accounts (vobiz, telephony) that read it.
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
from .provider_configs import STORED_SENTINEL, mask_enc

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


def _normalize_scalar_or_list(
    field_name: str, value: Any, old_value: Any, *, allow_pointer_schemes: bool,
) -> Any:
    """Seals a scalar sensitive field (Vobiz's auth_token) or every entry
    of a list-valued one (Cloudonix's api_keys): plaintext is encrypted,
    `"[stored]"` or a byte-identical `enc:` entry keeps what the row already
    holds, and any other `enc:` is refused. `telephony_configs.credentials`
    is tenant-writable JSONB, and the shared resolvers' `env:`/`k8s:` schemes
    were built for admin-entered infra config, not tenant input (lesson 37)."""
    if isinstance(value, list):
        old_list = old_value if isinstance(old_value, list) else []
        return [
            _normalize_one(
                field_name, entry, old_list[i] if i < len(old_list) else None,
                allow_pointer_schemes=allow_pointer_schemes,
            )
            for i, entry in enumerate(value)
        ]
    old_entry = old_value if isinstance(old_value, str) else None
    return _normalize_one(field_name, value, old_entry, allow_pointer_schemes=allow_pointer_schemes)


def _normalize_one(
    field_name: str, entry: Any, old_entry: str | None, *, allow_pointer_schemes: bool,
) -> str:
    if not isinstance(entry, str) or not entry:
        raise ValueError(f"{field_name} entries must be non-empty strings")
    if entry == STORED_SENTINEL:
        if old_entry is None:
            raise ValueError(f"{field_name}: credential_ref_not_accepted")
        return old_entry
    if is_encrypted(entry):
        if entry == old_entry:
            return entry
        raise ValueError(f"{field_name}: credential_ref_not_accepted")
    if entry.startswith(("env:", "k8s:")):
        if allow_pointer_schemes:
            return entry
        raise ValueError(f"{field_name}: credential_ref_not_accepted")
    return encrypt_secret(entry)


def _normalize_credentials(
    provider: str,
    credentials: dict[str, Any],
    old_credentials: dict[str, Any] | None = None,
    *,
    allow_pointer_schemes: bool,
) -> dict[str, Any]:
    """Provider-agnostic over `sensitive_credential_fields()` — scalar and
    list-valued fields both seal the same way. Native (5000-5009) has no
    ITelephonyProvider, so any provider's secret field name is sealed."""
    if provider not in _NON_REST_PROVIDERS:
        TelephonyProviderRegistry.get(provider)   # unknown provider raises
    old = old_credentials or {}
    sealed = dict(credentials)
    # Native has no provider class: a secret-named field sent with it is still sealed.
    native = provider in _NON_REST_PROVIDERS
    for field_name in _sensitive_fields(provider):
        if field_name in sealed and not (native and not sealed[field_name]):
            sealed[field_name] = _normalize_scalar_or_list(
                field_name, sealed[field_name], old.get(field_name),
                allow_pointer_schemes=allow_pointer_schemes,
            )
    return sealed


def _sensitive_fields(provider: str) -> frozenset[str]:
    """The provider's secret fields; for native or unknown providers, every provider's,
    since a row's credentials may have been saved under another provider."""
    providers = TelephonyProviderRegistry.all()
    if provider in providers:
        return frozenset(providers[provider].sensitive_credential_fields())
    return frozenset(f for cls in providers.values() for f in cls.sensitive_credential_fields())


def _mask_entry(value: Any, *, sensitive: bool) -> Any:
    if not isinstance(value, str):
        return value
    # A sensitive field is masked even if it was stored before sealing existed (plaintext).
    if sensitive and value and not value.startswith(("env:", "k8s:")):
        return STORED_SENTINEL
    return mask_enc(value)


def public_telephony_config(cfg: dict[str, Any], *, masked: bool) -> dict[str, Any]:
    """Masks every `enc:` string in credentials (covers fields a provider adds later)
    and every plaintext value of the provider's sensitive fields."""
    if not masked:
        return cfg
    sensitive = _sensitive_fields(cfg.get("provider", ""))
    credentials = {
        key: [_mask_entry(e, sensitive=key in sensitive) for e in value] if isinstance(value, list)
        else _mask_entry(value, sensitive=key in sensitive)
        for key, value in cfg["credentials"].items()
    }
    return {**cfg, "credentials": credentials}


async def seal_plaintext_credentials(*, tenant_id: Any | None = None) -> int:
    """Startup, idempotent: encrypts sensitive credentials stored before sealing existed
    (2026-09-25). Returns how many configs were sealed. tenant_id narrows it (tests)."""
    pool = await db.get_pool()
    sealed_ids = []
    async with platform_conn(pool, reason="telephony-configs-seal-plaintext") as conn:
        rows = await conn.fetch(
            "SELECT id, provider, credentials FROM telephony_configs "
            "WHERE deleted_at IS NULL AND ($1::uuid IS NULL OR tenant_id = $1) FOR UPDATE",
            tenant_id,
        )
        for row in rows:
            credentials = db.json_col(row["credentials"]) or {}
            fields = _sensitive_fields(row["provider"])
            if not any(_is_plaintext(credentials.get(f)) for f in fields):
                continue
            sealed = dict(credentials)
            for f in fields:
                if f in sealed:
                    sealed[f] = (
                        [encrypt_secret(e) if _is_plaintext(e) else e for e in sealed[f]]
                        if isinstance(sealed[f], list)
                        else encrypt_secret(sealed[f]) if _is_plaintext(sealed[f]) else sealed[f]
                    )
            await conn.execute(
                "UPDATE telephony_configs SET credentials = $2::jsonb WHERE id = $1",
                row["id"], _json.dumps(sealed),
            )
            sealed_ids.append(row["id"])
    for config_id in sealed_ids:
        await cache.invalidate(_cache_key(config_id))
    return len(sealed_ids)


def _is_plaintext(value: Any) -> bool:
    if isinstance(value, list):
        return any(_is_plaintext(e) for e in value)
    return (
        isinstance(value, str) and bool(value) and value != STORED_SENTINEL
        and not is_encrypted(value) and not value.startswith(("env:", "k8s:"))
    )


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
    allow_pointer_schemes: bool,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    credentials = _normalize_credentials(provider, credentials, allow_pointer_schemes=allow_pointer_schemes)
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
    allow_pointer_schemes: bool,
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
            old_credentials = db.json_col(old["credentials"]) or {}
            new_credentials = _normalize_credentials(
                old["provider"], fields["credentials"], old_credentials,
                allow_pointer_schemes=allow_pointer_schemes,
            )
            validate_credentials(old["provider"], new_credentials)
            # Ids the platform created at the provider (number_sync.py) aren't
            # in the admin's form; dropping them would orphan that resource.
            # Only within the same provider account: another account can't use them.
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
