from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from .. import tenants as tenants_service
from .. import users as users_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404, require_role
from ..schemas import TenantConcurrencyUpdate, TenantCreate, TenantTransferPolicyUpdate, TenantUpdate

router = APIRouter(prefix="/tenants", tags=["tenants"])
log = logging.getLogger(__name__)


async def _revoke_grants_upstream(tenant_id: str, grants: list[dict[str, Any]], authorization: str) -> None:
    """Best effort, after the response: the tokens are already cleared in the DB, so a failure
    here only means the provider grant stays alive. The deleting superadmin's own token is forwarded."""
    base = os.environ.get("TOOLEXEC_SERVICE_URL", "http://localhost:8600")
    try:
        async with httpx.AsyncClient(base_url=base.rstrip("/"), timeout=10.0) as client:
            resp = await client.post(
                "/internal/oauth/revoke-grants", json={"tenant_id": tenant_id, "grants": grants},
                headers={"Authorization": authorization},
            )
        if resp.status_code >= 300:
            log.warning("tenant_delete_revoke_rejected", extra={"tenant_id": tenant_id, "status": resp.status_code})
    except httpx.HTTPError as exc:
        log.warning("tenant_delete_revoke_unreachable", extra={"tenant_id": tenant_id, "error": type(exc).__name__})


@router.get("")
async def list_tenants(current_user: CurrentUser = Depends(get_current_user)):
    # Tenant-scoped actors see only their own tenant; platform-scoped (None) see all.
    return await tenants_service.list_tenants(tenant_id=current_user.tenant_id)


# Tenant create/rename/delete is platform-level: superadmin only.
@router.post("", status_code=201)
async def create_tenant(body: TenantCreate, current_user: CurrentUser = Depends(require_role("superadmin"))):
    return await tenants_service.create_tenant(
        name=body.name, slug=body.slug, region=body.region,
        user_id=current_user.id, user_email=current_user.email,
    )


@router.get("/{slug}")
async def get_tenant(slug: str, current_user: CurrentUser = Depends(get_current_user)):
    tenant = await get_or_404(tenants_service.get_tenant(slug), f"tenant {slug!r} not found")
    # 404, not 403, for a foreign tenant so this isn't an existence oracle.
    if current_user.tenant_id is not None and str(tenant["id"]) != str(current_user.tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {slug!r} not found")
    return tenant


@router.patch("/{tenant_id}")
async def update_tenant(
    tenant_id: str, body: TenantUpdate, current_user: CurrentUser = Depends(require_role("superadmin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await tenants_service.update_tenant(
        tenant_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.patch("/{tenant_id}/concurrency")
async def update_tenant_concurrency(
    tenant_id: str, body: TenantConcurrencyUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Admin-editable concurrency, separate so admins can't touch other tenant fields.
    Scope comes from the freshly read users row, not the token (a re-tenanted admin edits the new tenant)."""
    row = await users_service.get_user_by_id(current_user.id)
    if row is None:
        raise HTTPException(status_code=403, detail="account is no longer active")
    if row["role"] not in ("superadmin", "admin"):
        raise HTTPException(status_code=403, detail=f"role {row['role']!r} cannot perform this action")
    effective_tenant_id = str(row["tenant_id"]) if row["tenant_id"] is not None else None

    if row["role"] != "superadmin" and effective_tenant_id != tenant_id:
        # Same non-oracle 404 as GET /tenants/{slug}.
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id!r} not found")

    return await tenants_service.update_tenant(
        tenant_id, user_id=row["id"], user_email=row["email"],
        max_concurrent_calls=body.max_concurrent_calls,
    )


@router.patch("/{tenant_id}/transfer-policy")
async def update_tenant_transfer_policy(
    tenant_id: str, body: TenantTransferPolicyUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin")),
):
    """Superadmin only: international transfers are billed to the platform, so a tenant
    admin must not be able to switch them on or move what counts as domestic. The role is
    re-read from the users row, as for /concurrency, so a stale token cannot carry it."""
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    row = await users_service.get_user_by_id(current_user.id)
    if row is None:
        raise HTTPException(status_code=403, detail="account is no longer active")
    if row["role"] != "superadmin":
        raise HTTPException(status_code=403, detail=f"role {row['role']!r} cannot perform this action")
    return await tenants_service.update_tenant(
        tenant_id, user_id=row["id"], user_email=row["email"], **fields,
    )


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: str, request: Request, background_tasks: BackgroundTasks, force: bool = False,
    current_user: CurrentUser = Depends(require_role("superadmin")),
):
    grants = await tenants_service.soft_delete_tenant(
        tenant_id, user_id=current_user.id, user_email=current_user.email, force=force,
    )
    authorization = request.headers.get("authorization")
    if grants and authorization:
        background_tasks.add_task(_revoke_grants_upstream, tenant_id, grants, authorization)
