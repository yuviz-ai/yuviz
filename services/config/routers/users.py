from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from libs.tenancy import set_target_tenant

from .. import users as users_service
from ..auth import CurrentUser
from ..deps import (
    assert_tenant_access, forget_user, get_current_user, get_or_404, is_platform_scoped, require_role,
)
from ..schemas import UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
async def list_users(
    tenant_id: str | None = None, current_user: CurrentUser = Depends(get_current_user),
):
    # Scope isn't privilege: NULL-tenant viewer service accounts exist, so the cross-tenant
    # branch needs platform scope AND superadmin. Everyone else gets their own tenant.
    platform_scoped = is_platform_scoped(current_user) and current_user.role == "superadmin"
    scoped_tenant_id = tenant_id if platform_scoped else current_user.tenant_id
    if scoped_tenant_id is not None:
        set_target_tenant(scoped_tenant_id)
    users = await users_service.list_users(
        tenant_id=scoped_tenant_id, is_platform_scoped=platform_scoped,
    )
    return [users_service.to_public_dict(u) for u in users]


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("superadmin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")

    platform_scoped = is_platform_scoped(current_user)
    row = await get_or_404(
        users_service.get_user_for_admin(user_id, platform_scoped=platform_scoped),
        f"user {user_id!r} not found",
    )
    await assert_tenant_access(row["tenant_id"], current_user)
    # Check the written tenant_id too, or {"tenant_id": null} would self-promote to platform scope.
    if "tenant_id" in fields:
        await assert_tenant_access(fields["tenant_id"], current_user)

    if row["tenant_id"] is not None:
        set_target_tenant(row["tenant_id"])
    user = await users_service.update_user(
        user_id,
        actor_user_id=current_user.id,
        actor_user_email=current_user.email,
        row_tenant_id=row["tenant_id"],
        **fields,
    )
    # Drop memoized authority so gates that re-read the row see the change now, not after the TTL.
    if fields.keys() & {"password", "role", "tenant_id"}:
        forget_user(request.app.state, str(user["id"]))
    return users_service.to_public_dict(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str, current_user: CurrentUser = Depends(require_role("superadmin")),
):
    platform_scoped = is_platform_scoped(current_user)
    row = await get_or_404(
        users_service.get_user_for_admin(user_id, platform_scoped=platform_scoped),
        f"user {user_id!r} not found",
    )
    await assert_tenant_access(row["tenant_id"], current_user)

    if row["tenant_id"] is not None:
        set_target_tenant(row["tenant_id"])
    await users_service.soft_delete_user(
        user_id,
        actor_user_id=current_user.id,
        actor_user_email=current_user.email,
        row_tenant_id=row["tenant_id"],
    )
