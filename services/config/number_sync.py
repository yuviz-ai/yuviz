"""Keeps a REST provider's inbound routing in step with phone_numbers (cold path only).

A sync failure never loses the admin's change; create/update report it as `provider_sync`.
"""

from __future__ import annotations

import json as _json
import logging
import os
from typing import Any

from libs.config_sdk.secrets import decrypt_secret, is_encrypted
from libs.telephony_sdk.exceptions import TelephonyProviderError
from libs.telephony_sdk.interface import InboundUrls, ITelephonyProvider
from libs.telephony_sdk.registry import TelephonyProviderRegistry
from libs.tenancy import platform_conn

from . import audit, cache, db, telephony_configs

log = logging.getLogger(__name__)

PUBLIC_BASE_URL_ENV = "TELEPHONY_PUBLIC_BASE_URL"


class NumberNotInAccount(Exception):
    def __init__(self, did: str, provider: str) -> None:
        super().__init__(f"{did} isn't a number in this {provider} account")


class ProviderUnreachable(Exception):
    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"couldn't check the number with {provider}: {detail}")


def _decrypt(value: Any) -> Any:
    if isinstance(value, list):
        return [_decrypt(v) for v in value]
    if isinstance(value, str) and is_encrypted(value):
        return decrypt_secret(value)
    return value


def _provider_for(config: dict[str, Any]) -> ITelephonyProvider | None:
    name = config["provider"]
    if name == telephony_configs.NATIVE_PROVIDER or name not in TelephonyProviderRegistry.all():
        return None
    provider_cls = TelephonyProviderRegistry.get(name)
    credentials = dict(config.get("credentials") or {})
    for field in provider_cls.sensitive_credential_fields():
        if field in credentials:
            credentials[field] = _decrypt(credentials[field])
    return provider_cls(credentials)


def inbound_urls(config: dict[str, Any]) -> InboundUrls | None:
    base = os.environ.get(PUBLIC_BASE_URL_ENV, "").rstrip("/")
    if not base:
        return None
    path = f"{config['provider']}/{{}}/{config['id']}"
    return InboundUrls(answer_url=f"{base}/{path.format('voice')}", hangup_url=f"{base}/{path.format('status')}")


async def ensure_owned(config: dict[str, Any], did: str) -> None:
    """Refuses a number the provider says isn't in the account. A provider
    that can't check (None) is allowed through."""
    provider = _provider_for(config)
    if provider is None:
        return
    try:
        owned = await provider.owns_number(did)
    except TelephonyProviderError as exc:
        raise ProviderUnreachable(config["provider"], str(exc)) from None
    if owned is False:
        raise NumberNotInAccount(did, config["provider"])


async def attach(config: dict[str, Any], did: str, *, refresh_app: bool = True) -> dict[str, Any] | None:
    """None when the config has nothing to sync (native, carriers)."""
    provider = _provider_for(config)
    if provider is None:
        return None
    urls = inbound_urls(config)
    if urls is None:
        return {"ok": False, "message": f"{PUBLIC_BASE_URL_ENV} isn't set on the Config service, so the "
                                         "provider can't be pointed at this platform"}
    result = await provider.attach_inbound(did, urls, label=f"Yuviz - {config['name']}", refresh_app=refresh_app)
    if result.credentials_update and not await _merge_credentials(config, result.credentials_update):
        # A concurrent sync stored its own resource first: use that one, drop ours.
        winner = await telephony_configs.get_telephony_config(config["id"], platform_scoped=True)
        retried = await attach(winner, did, refresh_app=False) if winner is not None else None
        await provider.discard_inbound_resources(result.credentials_update)
        return retried
    if not result.ok:
        log.warning("number_sync: attach failed config=%s did=%s: %s", config["id"], did, result.message)
    return {"ok": result.ok, "message": result.message}


async def refresh(config: dict[str, Any]) -> dict[str, Any] | None:
    """Re-points the config's shared provider resource; None if there is none yet."""
    provider = _provider_for(config)
    urls = inbound_urls(config)
    if provider is None or urls is None:
        return None
    result = await provider.refresh_inbound(urls)
    if result is None:
        return None
    if not result.ok:
        log.warning("number_sync: refresh failed config=%s: %s", config["id"], result.message)
    return {"ok": result.ok, "message": result.message}


async def detach(config: dict[str, Any], did: str) -> dict[str, Any] | None:
    provider = _provider_for(config)
    if provider is None:
        return None
    result = await provider.detach_inbound(did)
    return {"ok": result.ok, "message": result.message}


async def _merge_credentials(config: dict[str, Any], update: dict[str, Any]) -> bool:
    """Compare-and-swap: stores `update` only if those keys still hold what
    `config` was read with. False means another sync changed them first."""
    seen = config.get("credentials") or {}
    params: list[Any] = [config["id"], _json.dumps(update), config["tenant_id"]]
    unchanged = []
    for key in update:
        params += [key, _json.dumps(seen.get(key))]
        unchanged.append(f"credentials -> ${len(params) - 1}::text IS NOT DISTINCT FROM NULLIF(${len(params)}::jsonb, 'null'::jsonb)")
    pool = await db.get_pool()
    async with platform_conn(pool, reason="number-sync-provider-ids", stamp_tenant=str(config["tenant_id"])) as conn:
        stored = await conn.fetchval(
            "UPDATE telephony_configs SET credentials = credentials || $2::jsonb, updated_at = now() "
            f"WHERE id = $1 AND tenant_id = $3 AND {' AND '.join(unchanged)} RETURNING true",
            *params,
        )
        if not stored:
            return False
        await audit.write_audit(
            conn, entity_type="telephony_config", entity_id=config["id"], action="updated",
            user_id=None, user_email="number-sync", new_value=update,
        )
    await cache.invalidate(telephony_configs._cache_key(config["id"]))
    return True
