from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from libs.tenancy import set_target_tenant

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


async def _resolve_tenant_id(tenant_id: str) -> None:
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


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
    """The Cloudonix service's cold-path account preload — platform-scoped
    only (`tenant_id is None`, never `role == "superadmin"`, lesson 24),
    same shape as Conversation's prewarm. `credentials.api_keys` comes back
    as sealed `enc:` Fernet tokens, worthless without
    SECRET_ENCRYPTION_KEY (same stance as provider_configs.py's note)."""
    if not is_platform_scoped(current_user):
        raise HTTPException(status_code=403, detail="platform-scoped access required")
    return await telephony_configs_service.list_configs_by_provider(provider)


async def _authorize_telephony_config(config_id: str, current_user: CurrentUser) -> dict:
    """Same shape as provider_configs._authorize_provider: 404 if missing,
    403 if it belongs to a different tenant. This is also the cached-read
    control (see design's "Caches and RLS") — telephony_configs.get_telephony_config
    can be satisfied entirely from Redis, so this app-layer check, not RLS,
    is what closes the by-id routes on a cache hit."""
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
    fields = body.model_dump(exclude_unset=True)
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
    set_target_tenant(cfg["tenant_id"])
    return await telephony_configs_service.set_default_outbound(
        config_id, user_id=current_user.id, user_email=current_user.email,
    )


@router.delete("/{config_id}", status_code=204)
async def delete_telephony_config(
    config_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_telephony_config(config_id, current_user)
    set_target_tenant(cfg["tenant_id"])
    await telephony_configs_service.soft_delete_telephony_config(
        config_id, user_id=current_user.id, user_email=current_user.email,
    )


@providers_router.get("/telephony-providers")
async def list_supported_providers(current_user: CurrentUser = Depends(get_current_user)):
    """Discovery endpoint (Dograh's "List Supported Providers" equivalent) —
    name -> required credential fields, so an admin UI can render the right
    form per provider without hardcoding field lists."""
    return telephony_configs_service.list_supported_providers()
