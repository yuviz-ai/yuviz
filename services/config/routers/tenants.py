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
    # Scope to the actor's own tenant for anyone who isn't platform-scoped.
    # current_user.tenant_id comes from the verified JWT, never a client-
    # supplied value — same pattern as GET /users and GET /invites. Only
    # None (superadmin, or a service account — see tenants_service.
    # list_tenants' docstring) sees every tenant; a tenant-scoped
    # admin/viewer's tenant_id narrows this to a single-entry list, which
    # is also all the invite-create tenant picker needs (may_invite already
    # forbids inviting into any other tenant).
    return await tenants_service.list_tenants(tenant_id=current_user.tenant_id)


# Creating/renaming/deleting a tenant is a platform-level action — superadmin
# only, unlike per-tenant resources (agents, providers, ...) where a
# tenant-scoped admin can act within their own tenant.
@router.post("", status_code=201)
async def create_tenant(body: TenantCreate, current_user: CurrentUser = Depends(require_role("superadmin"))):
    return await tenants_service.create_tenant(
        name=body.name, slug=body.slug, region=body.region,
        user_id=current_user.id, user_email=current_user.email,
    )


@router.get("/{slug}")
async def get_tenant(slug: str, current_user: CurrentUser = Depends(get_current_user)):
    tenant = await get_or_404(tenants_service.get_tenant(slug), f"tenant {slug!r} not found")
    # Same scoping as list_tenants above, and the identical 404 (never
    # 403) for a foreign tenant that a tenant-scoped actor can't see — the
    # slug existing at all must not be a distinguishable outcome from it
    # not existing (no existence oracle).
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
    """AC16's admin-editable path — a dedicated route rather than widening
    PATCH /tenants/{tenant_id} (superadmin-only), which would hand an admin
    name/region/VAD/default-provider ids too.

    The own-tenant check re-reads the actor's CURRENT role/tenant_id from
    `users` rather than trusting `current_user.tenant_id` off the token
    (closes security finding #1 — same class of bug as the two highs this
    whole feature exists to fix: a JWT claim is a login-time snapshot, not a
    live fact). This deliberately does NOT reuse deps.assert_current_authority:
    that helper 403s outright the instant the fresh row's tenant no longer
    matches the TOKEN's claim, which is the right contract for the live-calls
    intervention route (any re-tenanting kills that write), but wrong here —
    an admin who has just been transferred to a new tenant must still be able
    to edit THAT tenant's concurrency with their still-valid token, scoped to
    the tenant the fresh row says they belong to now, never the one the stale
    token remembers."""
    row = await users_service.get_user_by_id(current_user.id)
    if row is None:
        raise HTTPException(status_code=403, detail="account is no longer active")
    if row["role"] not in ("superadmin", "admin"):
        raise HTTPException(status_code=403, detail=f"role {row['role']!r} cannot perform this action")
    effective_tenant_id = str(row["tenant_id"]) if row["tenant_id"] is not None else None

    if row["role"] != "superadmin" and effective_tenant_id != tenant_id:
        # Same non-oracle 404 body GET /tenants/{slug} already returns for a
        # foreign tenant — a re-tenanted admin querying their OLD tenant id
        # is indistinguishable from one that never existed (lesson 2).
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id!r} not found")

    return await tenants_service.update_tenant(
        tenant_id, user_id=row["id"], user_email=row["email"],
        max_concurrent_calls=body.max_concurrent_calls,
    )


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(tenant_id: str, current_user: CurrentUser = Depends(require_role("superadmin"))):
    await tenants_service.soft_delete_tenant(tenant_id, user_id=current_user.id, user_email=current_user.email)
