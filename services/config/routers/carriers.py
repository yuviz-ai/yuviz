from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant

from .. import carriers as carriers_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..carriers import public_carrier
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
from ..provider_configs import ref_mask_required
from ..schemas import CarrierCreate, CarrierUpdate

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/carriers",
    tags=["carriers"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/carriers", tags=["carriers"])


async def _resolve_tenant_id(tenant_id: str) -> None:
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


@tenant_scoped_router.get("")
async def list_carriers(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    rows = await carriers_service.list_carriers(tenant_id)
    return [public_carrier(r, masked=ref_mask_required(current_user)) for r in rows]


@tenant_scoped_router.post("", status_code=201)
async def create_carrier(
    tenant_id: str,
    body: CarrierCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant_id(tenant_id)
    created = await carriers_service.create_carrier(
        tenant_id=tenant_id,
        name=body.name,
        provider=body.provider,
        auth_id=body.auth_id,
        auth_token_ref=body.auth_token_ref,
        auth_token=body.auth_token.get_secret_value() if body.auth_token else None,
        carrier_account_ref=body.carrier_account_ref,
        allow_pointer_schemes=is_platform_scoped(current_user),
        user_id=current_user.id,
        user_email=current_user.email,
    )
    return public_carrier(created, masked=ref_mask_required(current_user))


@router.get("/{carrier_id}")
async def get_carrier(carrier_id: str, current_user: CurrentUser = Depends(get_current_user)):
    platform_scoped = is_platform_scoped(current_user)
    carrier = await get_or_404(
        carriers_service.get_carrier_by_id(carrier_id, platform_scoped=platform_scoped),
        f"carrier {carrier_id!r} not found",
    )
    await assert_tenant_access(carrier["tenant_id"], current_user)
    return public_carrier(carrier, masked=ref_mask_required(current_user))


@router.patch("/{carrier_id}")
async def update_carrier(
    carrier_id: str,
    body: CarrierUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    carrier = await get_or_404(
        carriers_service.get_carrier_by_id(carrier_id, platform_scoped=is_platform_scoped(current_user)),
        f"carrier {carrier_id!r} not found",
    )
    await assert_tenant_access(carrier["tenant_id"], current_user)
    set_target_tenant(carrier["tenant_id"])
    if fields.get("auth_token") is not None:
        fields["auth_token"] = fields["auth_token"].get_secret_value()
    updated = await carriers_service.update_carrier(
        carrier_id,
        allow_pointer_schemes=is_platform_scoped(current_user),
        user_id=current_user.id,
        user_email=current_user.email,
        **fields,
    )
    return public_carrier(updated, masked=ref_mask_required(current_user))


@router.delete("/{carrier_id}", status_code=204)
async def delete_carrier(
    carrier_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    carrier = await get_or_404(
        carriers_service.get_carrier_by_id(carrier_id, platform_scoped=is_platform_scoped(current_user)),
        f"carrier {carrier_id!r} not found",
    )
    await assert_tenant_access(carrier["tenant_id"], current_user)
    set_target_tenant(carrier["tenant_id"])
    try:
        await carriers_service.soft_delete_carrier(
            carrier_id, user_id=current_user.id, user_email=current_user.email,
        )
    except carriers_service.CarrierHasNumbers as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
