from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import tenants as tenants_service
from .. import users as users_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404, require_role
from ..schemas import TenantConcurrencyUpdate, TenantCreate, TenantUpdate

router = APIRouter(prefix="/tenants", tags=["tenants"])


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


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: str, force: bool = False, current_user: CurrentUser = Depends(require_role("superadmin")),
):
    await tenants_service.soft_delete_tenant(
        tenant_id, user_id=current_user.id, user_email=current_user.email, force=force,
    )
