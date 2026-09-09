"""
services/toolexec/routers/custom_apis.py — the admin surface (T16).

Auth on every route: reads behind plain `Depends(get_current_user)`, writes
behind `Depends(require_role("superadmin","admin"))` — the same
services/config/deps.py dependencies services/knowledge/routers/
knowledge_bases.py uses, never a bare authenticated check (that would hand
supervisor/agent — every other console role — read+write access, lesson
4). Tenant scope for the `/tenants/{tenant_id}/...` routes is
`_require_tenant_access` (below, adapted from
services/config/routers/provider_configs.py:68 to use
`deps.is_platform_scoped` rather than a role comparison — lesson 24); the
id-addressed `/custom-apis/{id}` routes are scoped by `_authorize_custom_api`.

Both helpers raise LookupError with the SAME fixed detail string for "no
such id" and "exists, but not your tenant" (lesson 2) — app.py's existing
LookupError handler turns that into a 404 with no extra wiring needed here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import get_current_user, is_platform_scoped, require_role

from .. import custom_apis as custom_apis_service
from ..schemas import CustomApiCreate, CustomApiUpdate

tenant_scoped_router = APIRouter(prefix="/tenants/{tenant_id}/custom-apis", tags=["custom_apis"])
router = APIRouter(prefix="/custom-apis", tags=["custom_apis"])

_NOT_FOUND_DETAIL = "custom_api not found"


def _require_tenant_access(tenant_id: str, current_user: CurrentUser) -> None:
    """403 (not 404): the caller IS authenticated and the tenant_id in the
    path DOES exist, they simply aren't allowed to act on it — there is no
    id to avoid leaking the existence of here, unlike _authorize_custom_api
    below."""
    if not is_platform_scoped(current_user) and tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="tenant_id does not match the caller's tenant")


async def _authorize_custom_api(custom_api_id: str, current_user: CurrentUser) -> dict[str, Any]:
    """404, identical detail, for both 'no such id' and 'exists but is
    another tenant's' — deliberately diverging from provider_configs.py's
    403 for the equivalent case, which lets a tenant admin probe whether an
    opaque id exists elsewhere on the platform (lesson 2)."""
    api = await custom_apis_service.get_custom_api(custom_api_id)
    if api is None:
        raise LookupError(_NOT_FOUND_DETAIL)
    if str(api["tenant_id"]) != current_user.tenant_id and not is_platform_scoped(current_user):
        raise LookupError(_NOT_FOUND_DETAIL)
    return api


@tenant_scoped_router.get("")
async def list_custom_apis(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    _require_tenant_access(tenant_id, current_user)
    return await custom_apis_service.list_custom_apis(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_custom_api(
    tenant_id: str,
    body: CustomApiCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    _require_tenant_access(tenant_id, current_user)
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
    return await _authorize_custom_api(custom_api_id, current_user)


@router.patch("/{custom_api_id}")
async def update_custom_api(
    custom_api_id: str,
    body: CustomApiUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_custom_api(custom_api_id, current_user)
    fields = body.model_dump(exclude_unset=True, exclude={"params"})
    if not fields and body.params is None:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    params = [p.model_dump() for p in body.params] if body.params is not None else None
    return await custom_apis_service.update_custom_api(
        custom_api_id, params=params, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{custom_api_id}", status_code=204)
async def delete_custom_api(
    custom_api_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_custom_api(custom_api_id, current_user)
    await custom_apis_service.soft_delete_custom_api(
        custom_api_id, user_id=current_user.id, user_email=current_user.email,
    )
