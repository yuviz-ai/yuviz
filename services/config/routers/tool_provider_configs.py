from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from libs.tenancy import set_target_tenant

from .. import tenants as tenants_service
from .. import tool_provider_configs as tool_provider_configs_service
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
from ..schemas import ToolProviderConfigCreate, ToolProviderConfigUpdate

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/tool-providers",
    tags=["tool_provider_configs"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/tool-providers", tags=["tool_provider_configs"])


async def _resolve_tenant_id(tenant_id: str) -> None:
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


@tenant_scoped_router.get("")
async def list_tool_provider_configs(
    tenant_id: str,
    tool_name: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await tool_provider_configs_service.list_tool_provider_configs(tenant_id, tool_name=tool_name)


@tenant_scoped_router.post("", status_code=201)
async def create_tool_provider_config(
    tenant_id: str,
    body: ToolProviderConfigCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant_id(tenant_id)
    # engine='toolexec' is internal infrastructure (services/toolexec/), not
    # a tenant credential — it has no api_key_ref to require. Every other
    # engine still needs one.
    if body.engine != "toolexec" and not ((body.api_key_ref or "").strip() or (body.api_key or "").strip()):
        raise HTTPException(status_code=400, detail="api_key_ref or api_key is required")
    return await tool_provider_configs_service.create_tool_provider_config(
        tenant_id=tenant_id,
        name=body.name,
        tool_name=body.tool_name,
        engine=body.engine,
        api_key_ref=body.api_key_ref,
        api_key=body.api_key,
        extra=body.extra,
        user_id=current_user.id,
        user_email=current_user.email,
    )


async def _authorize_tool_provider(tool_provider_config_id: str, current_user: CurrentUser) -> dict:
    """404 if missing; 403 if it exists but belongs to a different tenant —
    same shared predicate/shapes as deps.assert_tenant_access (lesson 24:
    is_platform_scoped, not role == "superadmin")."""
    platform_scoped = is_platform_scoped(current_user)
    cfg = await get_or_404(
        tool_provider_configs_service.get_tool_provider_config(
            tool_provider_config_id, platform_scoped=platform_scoped,
        ),
        f"tool_provider_config {tool_provider_config_id!r} not found",
    )
    await assert_tenant_access(cfg["tenant_id"], current_user)
    return cfg


@router.get("/{tool_provider_config_id}")
async def get_tool_provider_config(
    tool_provider_config_id: str, current_user: CurrentUser = Depends(get_current_user),
):
    return await _authorize_tool_provider(tool_provider_config_id, current_user)


@router.patch("/{tool_provider_config_id}")
async def update_tool_provider_config(
    tool_provider_config_id: str,
    body: ToolProviderConfigUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_tool_provider(tool_provider_config_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    # A cleared key fails silently until the next real call to this
    # provider — allowed only when paired with a real replacement in the
    # same request (a rotation, not a clear).
    if "api_key_ref" in fields and not (fields["api_key_ref"] or "").strip() and not (fields.get("api_key") or "").strip():
        raise HTTPException(status_code=400, detail="api_key_ref must not be blank")
    set_target_tenant(cfg["tenant_id"])
    return await tool_provider_configs_service.update_tool_provider_config(
        tool_provider_config_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{tool_provider_config_id}", status_code=204)
async def delete_tool_provider_config(
    tool_provider_config_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_tool_provider(tool_provider_config_id, current_user)
    set_target_tenant(cfg["tenant_id"])
    await tool_provider_configs_service.soft_delete_tool_provider_config(
        tool_provider_config_id, user_id=current_user.id, user_email=current_user.email,
    )
