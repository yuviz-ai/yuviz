from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from libs.tenancy import set_target_tenant

from .. import number_sync
from .. import phone_numbers as phone_numbers_service
from .. import telephony_configs as telephony_configs_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    get_or_404,
    is_platform_scoped,
    require_path_tenant_access,
    require_role,
    validate_id_exists,
)
from ..schemas import TelephonyConfigCreate, TelephonyConfigUpdate

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/telephony-configs",
    tags=["telephony_configs"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/telephony-configs", tags=["telephony_configs"])
providers_router = APIRouter(tags=["telephony_configs"])

_SYNC_CONCURRENCY = 5


async def _resolve_tenant_id(tenant_id: str) -> None:
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


_NATIVE_NOT_DEFAULT_OUTBOUND = (
    "a Native configuration can't be the default for outbound calls: that default is used to place "
    "calls through a REST provider, and native numbers dial through the platform's FreeSWITCH"
)


def _require_superadmin_for_native(provider: str, current_user: CurrentUser) -> None:
    # Native is the platform's shared Kamailio/FreeSWITCH, not a tenant's own
    # account: local numbers and their config are assigned by the platform.
    if provider == telephony_configs_service.NATIVE_PROVIDER and current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Native (local SIP) configurations are managed by the platform")


@tenant_scoped_router.get("")
async def list_telephony_configs(
    tenant_id: str, current_user: CurrentUser = Depends(get_current_user),
):
    return await telephony_configs_service.list_telephony_configs(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_telephony_config(
    tenant_id: str,
    body: TelephonyConfigCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    _require_superadmin_for_native(body.provider, current_user)
    if body.provider == telephony_configs_service.NATIVE_PROVIDER and body.is_default_outbound:
        raise HTTPException(status_code=400, detail=_NATIVE_NOT_DEFAULT_OUTBOUND)
    await _resolve_tenant_id(tenant_id)
    return await telephony_configs_service.create_telephony_config(
        tenant_id=tenant_id,
        name=body.name,
        provider=body.provider,
        credentials=body.credentials,
        is_default_outbound=body.is_default_outbound,
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("")
async def list_telephony_configs_by_provider(
    provider: str = Query(...), current_user: CurrentUser = Depends(get_current_user),
):
    """Platform-scoped account preload for telephony services; api_keys stay sealed `enc:` tokens."""
    if not is_platform_scoped(current_user):
        raise HTTPException(status_code=403, detail="platform-scoped access required")
    return await telephony_configs_service.list_configs_by_provider(provider)


async def _authorize_telephony_config(config_id: str, current_user: CurrentUser) -> dict:
    """404 if missing, 403 if another tenant's. Reads can hit Redis, so this check, not RLS, is the guard."""
    platform_scoped = is_platform_scoped(current_user)
    cfg = await get_or_404(
        telephony_configs_service.get_telephony_config(config_id, platform_scoped=platform_scoped),
        f"telephony_config {config_id!r} not found",
    )
    await assert_tenant_access(cfg["tenant_id"], current_user)
    return cfg


@router.get("/{config_id}")
async def get_telephony_config(config_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await _authorize_telephony_config(config_id, current_user)


@router.patch("/{config_id}")
async def update_telephony_config(
    config_id: str,
    body: TelephonyConfigUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_telephony_config(config_id, current_user)
    _require_superadmin_for_native(cfg["provider"], current_user)
    fields = body.model_dump(exclude_unset=True)
    if cfg["provider"] == telephony_configs_service.NATIVE_PROVIDER and fields.get("is_default_outbound"):
        raise HTTPException(status_code=400, detail=_NATIVE_NOT_DEFAULT_OUTBOUND)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    set_target_tenant(cfg["tenant_id"])
    return await telephony_configs_service.update_telephony_config(
        config_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.post("/{config_id}/set-default-outbound")
async def set_default_outbound(
    config_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_telephony_config(config_id, current_user)
    _require_superadmin_for_native(cfg["provider"], current_user)
    if cfg["provider"] == telephony_configs_service.NATIVE_PROVIDER:
        raise HTTPException(status_code=400, detail=_NATIVE_NOT_DEFAULT_OUTBOUND)
    set_target_tenant(cfg["tenant_id"])
    return await telephony_configs_service.set_default_outbound(
        config_id, user_id=current_user.id, user_email=current_user.email,
    )


@router.post("/{config_id}/sync-numbers")
async def sync_numbers(
    config_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Re-points every number on this config at the platform, e.g. after the
    public base URL changed."""
    cfg = await _authorize_telephony_config(config_id, current_user)
    set_target_tenant(cfg["tenant_id"])
    numbers = [
        n for n in await phone_numbers_service.list_phone_numbers(cfg["tenant_id"])
        if str(n.get("telephony_config_id")) == str(cfg["id"])
    ]
    results: list[dict] = []

    async def sync_one(number: dict, config: dict, refresh_app: bool) -> None:
        sync = await number_sync.attach(config, number["did"], refresh_app=refresh_app)
        if sync is not None:
            await phone_numbers_service.record_provider_sync(number["id"], number["tenant_id"], sync)
            results.append({"did": number["did"], **sync})

    # Refreshed once on its own, so no single number's failure can skip it.
    application = await number_sync.refresh(cfg)
    rest = numbers
    if application is None and numbers:
        # No shared app yet: the first attach creates it with the current URLs.
        await sync_one(numbers[0], cfg, refresh_app=False)
        cfg = await telephony_configs_service.get_telephony_config(config_id, platform_scoped=True)
        rest = numbers[1:]
    limit = asyncio.Semaphore(_SYNC_CONCURRENCY)

    async def bounded(number: dict) -> None:
        async with limit:
            await sync_one(number, cfg, refresh_app=False)

    await asyncio.gather(*(bounded(n) for n in rest))
    return {"results": results, "application": application}


@router.delete("/{config_id}", status_code=204)
async def delete_telephony_config(
    config_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_telephony_config(config_id, current_user)
    _require_superadmin_for_native(cfg["provider"], current_user)
    set_target_tenant(cfg["tenant_id"])
    await telephony_configs_service.soft_delete_telephony_config(
        config_id, user_id=current_user.id, user_email=current_user.email,
    )


@providers_router.get("/telephony-providers")
async def list_supported_providers(current_user: CurrentUser = Depends(get_current_user)):
    """Provider name -> required credential fields, for rendering per-provider forms."""
    return telephony_configs_service.list_supported_providers()
