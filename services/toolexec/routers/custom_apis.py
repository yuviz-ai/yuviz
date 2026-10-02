"""Custom APIs admin routes: reads need any user, writes need superadmin/admin.

Missing and cross-tenant ids both raise LookupError with the same detail (404) to avoid existence probing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    is_platform_scoped,
    require_path_tenant_access,
    require_role,
)

from .. import custom_apis as custom_apis_service
from ..schemas import CustomApiCreate, CustomApiUpdate

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/custom-apis",
    tags=["custom_apis"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/custom-apis", tags=["custom_apis"])

_NOT_FOUND_DETAIL = "custom_api not found"


async def _authorize_custom_api(
    custom_api_id: str, current_user: CurrentUser, *, platform_scoped: bool = False,
) -> dict[str, Any]:
    """Fetch and tenant-check a custom API; 404 for both missing and cross-tenant ids."""
    api = await custom_apis_service.get_custom_api(custom_api_id, platform_scoped=platform_scoped)
    if api is None:
        raise LookupError(_NOT_FOUND_DETAIL)
    if str(api["tenant_id"]) != current_user.tenant_id and not is_platform_scoped(current_user):
        raise LookupError(_NOT_FOUND_DETAIL)
    return api


@tenant_scoped_router.get("")
async def list_custom_apis(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await assert_tenant_access(tenant_id, current_user)
    return await custom_apis_service.list_custom_apis(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_custom_api(
    tenant_id: str,
    body: CustomApiCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await assert_tenant_access(tenant_id, current_user)
    return await custom_apis_service.create_custom_api(
        tenant_id=tenant_id,
        name=body.name,
        description=body.description,
        endpoint_url=body.endpoint_url,
        method=body.method,
        body_style=body.body_style,
        auth_scheme=body.auth_scheme,
        auth_config=body.auth_config,
        side_effecting=body.side_effecting,
        idempotency_header=body.idempotency_header,
        timeout_ms=body.timeout_ms,
        sensitive_response_paths=body.sensitive_response_paths,
        success_template=body.success_template,
        params=[p.model_dump() for p in body.params],
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("/{custom_api_id}")
async def get_custom_api(custom_api_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await _authorize_custom_api(
        custom_api_id, current_user, platform_scoped=is_platform_scoped(current_user),
    )


@router.patch("/{custom_api_id}")
async def update_custom_api(
    custom_api_id: str,
    body: CustomApiUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_custom_api(
        custom_api_id, current_user, platform_scoped=is_platform_scoped(current_user),
    )
    fields = body.model_dump(exclude_unset=True, exclude={"params"})
    if not fields and body.params is None:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    params = [p.model_dump() for p in body.params] if body.params is not None else None
    return await custom_apis_service.update_custom_api(
        custom_api_id, platform_scoped=is_platform_scoped(current_user),
        params=params, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{custom_api_id}", status_code=204)
async def delete_custom_api(
    custom_api_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_custom_api(
        custom_api_id, current_user, platform_scoped=is_platform_scoped(current_user),
    )
    await custom_apis_service.soft_delete_custom_api(
        custom_api_id, platform_scoped=is_platform_scoped(current_user),
        user_id=current_user.id, user_email=current_user.email,
    )
