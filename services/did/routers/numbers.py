"""
Numbers router — search/purchase/release, the DID Service's whole REST
surface (see project memory did-management-platform-architecture).

Full flow, split across two services on purpose (principle #7 — DID
Service talks to carriers, Config Service records DID->agent routing):
  1. GET  .../search    — live carrier lookup, no DB write.
  2. POST .../purchase  — carrier purchase, then THIS service writes its
                          own purchased_numbers row (unassigned).
  3. Admin UI calls Config Service's existing POST
     /tenants/{id}/phone-numbers directly to assign the number to an
     agent — DID Service is not involved in that step at all — then calls
     PATCH .../assign here just to link the two records for display.
  4. POST .../release   — carrier release, then this service marks its
                          own row released_at (and Config Service's
                          phone_numbers row, if assigned, is deleted by the
                          Admin UI the same way any DID removal already
                          works today).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    get_or_404,
    is_platform_scoped,
    require_path_tenant_access,
    require_role,
)

from .. import purchased_numbers as purchased_numbers_service
from ..carriers import get_carrier_by_id
from ..provider_manager import DidProviderManager
from ..providers.interface import DidProviderError
from ..runtime import get_provider_manager
from ..schemas import PurchaseNumberRequest

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/numbers",
    tags=["numbers"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/numbers", tags=["numbers"])


async def _resolve_carrier(carrier_id: str) -> dict:
    carrier = await get_carrier_by_id(carrier_id)
    if carrier is None:
        raise HTTPException(status_code=404, detail=f"carrier {carrier_id!r} not found")
    return carrier


@tenant_scoped_router.get("/search")
async def search_available_numbers(
    tenant_id: str,
    carrier_id: str = Query(...),
    country: str = Query(...),
    area_code: str | None = Query(default=None),
    limit: int = Query(default=10, le=50),
    current_user: CurrentUser = Depends(get_current_user),
    provider_manager: DidProviderManager = Depends(get_provider_manager),
):
    carrier = await _resolve_carrier(carrier_id)
    try:
        provider = await provider_manager.get(carrier)
        results = await provider.search_available_numbers(country, area_code, limit)
    except DidProviderError as exc:
        raise HTTPException(status_code=502, detail=f"carrier search failed: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return [
        {
            "phone_number": r.phone_number, "region": r.region,
            "monthly_price": r.monthly_price, "capabilities": list(r.capabilities),
        }
        for r in results
    ]


@tenant_scoped_router.post("/purchase", status_code=201)
async def purchase_number(
    tenant_id: str,
    body: PurchaseNumberRequest,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
    provider_manager: DidProviderManager = Depends(get_provider_manager),
):
    carrier = await _resolve_carrier(body.carrier_id)
    try:
        provider = await provider_manager.get(carrier)
        purchased = await provider.purchase_number(body.phone_number)
    except DidProviderError as exc:
        raise HTTPException(status_code=502, detail=f"carrier purchase failed: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await purchased_numbers_service.record_purchase(
        tenant_id=tenant_id,
        carrier_id=body.carrier_id,
        phone_number=purchased.phone_number,
        carrier_number_sid=purchased.carrier_number_sid,
        user_id=current_user.id,
        user_email=current_user.email,
    )


@tenant_scoped_router.get("")
async def list_purchased_numbers(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await purchased_numbers_service.list_purchased_numbers(tenant_id)


@router.patch("/{purchased_number_id}/assign")
async def assign_purchased_number(
    purchased_number_id: str,
    phone_number_id: str = Query(...),
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Links this purchased_numbers row to a phone_numbers row the caller
    already created via Config Service — see module docstring, step 3.
    This endpoint does not create or validate the phone_numbers row
    itself; that's Config Service's job."""
    platform_scoped = is_platform_scoped(current_user)
    purchased = await get_or_404(
        purchased_numbers_service.get_purchased_number(purchased_number_id, platform_scoped=platform_scoped),
        f"purchased_number {purchased_number_id!r} not found",
    )
    await assert_tenant_access(str(purchased["tenant_id"]), current_user)
    set_target_tenant(str(purchased["tenant_id"]))
    await purchased_numbers_service.record_assignment(purchased_number_id, phone_number_id)
    return await purchased_numbers_service.get_purchased_number(purchased_number_id)


@router.post("/{purchased_number_id}/release", status_code=200)
async def release_number(
    purchased_number_id: str,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
    provider_manager: DidProviderManager = Depends(get_provider_manager),
):
    platform_scoped = is_platform_scoped(current_user)
    purchased = await get_or_404(
        purchased_numbers_service.get_purchased_number(purchased_number_id, platform_scoped=platform_scoped),
        f"purchased_number {purchased_number_id!r} not found",
    )
    await assert_tenant_access(str(purchased["tenant_id"]), current_user)
    set_target_tenant(str(purchased["tenant_id"]))
    carrier = await _resolve_carrier(str(purchased["carrier_id"]))
    try:
        provider = await provider_manager.get(carrier)
        await provider.release_number(purchased["carrier_number_sid"])
    except DidProviderError as exc:
        raise HTTPException(status_code=502, detail=f"carrier release failed: {exc}")

    return await purchased_numbers_service.record_release(
        purchased_number_id, user_id=current_user.id, user_email=current_user.email,
    )
