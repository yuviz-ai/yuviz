from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant

from .. import users as users_service
from ..auth import CurrentUser
from ..deps import assert_tenant_access, get_current_user, get_or_404, is_platform_scoped, require_role
from ..schemas import UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
async def list_users(
    tenant_id: str | None = None, current_user: CurrentUser = Depends(get_current_user),
):
    # is_platform_scoped(current_user) (lesson 24: tenant_id is None) only
    # answers *which tenant* an actor is scoped to — it is a different
    # question from whether that actor is *privileged* to read across
    # every tenant. This route has no authority gate at all, only
    # CONSOLE_ROLES (get_current_user), so a NULL-tenant viewer-role
    # service account (Conversation, vobiz) is just as platform-scoped as
    # a superadmin. Both signals are required for the unscoped,
    # cross-tenant, role-unfiltered branch: `?tenant_id=` is honored, and
    # is_platform_scoped=True is passed to the service, only when the
    # actor is *also* superadmin. Anyone else — platform-scoped or not —
    # is forced to their own tenant_id (still NULL for that service
    # account) with users_service.list_users' `role != 'superadmin'`
    # exclusion applied, same as before this PR.
    platform_scoped = is_platform_scoped(current_user) and current_user.role == "superadmin"
    scoped_tenant_id = tenant_id if platform_scoped else current_user.tenant_id
    # tenant_conn()'s ambient GUC only follows the caller's own tenant
    # unless a platform-scoped actor's target is set explicitly — a
    # superadmin filtering to another tenant via ?tenant_id= needs this so
    # users.list_users' tenant_conn() branch resolves to THAT tenant.
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
    # Check the value being WRITTEN, not just the row being read — this is
    # what closes self-promotion: {"tenant_id": null} is a 403 for anyone
    # not already platform-scoped, and a privilege no-op for anyone who is
    # (assert_tenant_access admits None only for a platform-scoped caller).
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
