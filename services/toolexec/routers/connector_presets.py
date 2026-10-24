"""
services/toolexec/routers/connector_presets.py — the preset catalogue and its
apply and remove routes. The tenant is always the path's. Reads sit behind
`get_current_user`, writes behind `require_role("superadmin","admin")`, and
every write awaits `assert_tenant_access` first (lesson 38). Every custom-API
dict returned goes through `public_custom_api`, so no `*_ref` leaves the
service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from services.config.auth import CurrentUser
from services.config.deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    require_path_tenant_access,
    require_role,
)

from .. import custom_apis as custom_apis_service
from .. import oauth
from .. import presets
from ..schemas import PresetApplyRequest

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/connector-presets",
    tags=["connector_presets"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(tags=["connector_presets"])


@router.get("/connector-presets")
async def list_connector_presets(current_user: CurrentUser = Depends(get_current_user)):
    return [
        {"key": p.key, "title": p.title, "provider": p.provider, "setup_schema": p.setup_model.model_json_schema()}
        for p in presets.PRESETS.values()
    ]


@tenant_scoped_router.post("/{preset_key}/apply", status_code=201)
async def apply_connector_preset(
    tenant_id: str,
    preset_key: str,
    body: PresetApplyRequest,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await assert_tenant_access(tenant_id, current_user)
    await oauth.require_live_tenant(tenant_id)
    if preset_key not in presets.PRESETS:
        raise LookupError(preset_key)
    if body.preset_key != preset_key:
        raise HTTPException(status_code=400, detail="preset_key_mismatch")
    rows = await presets.apply_preset(
        tenant_id=tenant_id, preset_key=preset_key, setup=body,
        user_id=current_user.id, user_email=current_user.email,
    )
    return [custom_apis_service.public_custom_api(row) for row in rows]


@tenant_scoped_router.delete("/{preset_key}", status_code=204)
async def remove_connector_preset(
    tenant_id: str,
    preset_key: str,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await assert_tenant_access(tenant_id, current_user)
    if preset_key not in presets.PRESETS:
        raise LookupError(preset_key)
    await presets.remove_preset(
        tenant_id=tenant_id, preset_key=preset_key, user_id=current_user.id, user_email=current_user.email,
    )
    return Response(status_code=204)
