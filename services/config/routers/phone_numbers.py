from __future__ import annotations

import logging
import re

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from libs.tenancy import set_target_tenant

from .. import agents as agents_service
from .. import carriers as carriers_service
from .. import number_sync
from .. import phone_numbers as phone_numbers_service
from .. import telephony_configs as telephony_configs_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
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
from ..schemas import PhoneNumberCreate, PhoneNumberUpdate

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/phone-numbers",
    tags=["phone_numbers"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/phone-numbers", tags=["phone_numbers"])
log = logging.getLogger(__name__)


async def _resolve_tenant_id(tenant_id: str) -> None:
    """Clean 400/404 instead of an FK violation surfacing as a 500."""
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


async def _resolve_agent_id(agent_id: str | None) -> None:
    await validate_id_exists(agent_id, agents_service.get_agent_by_id, "agent")


async def _resolve_carrier_id(carrier_id: str | None) -> None:
    await validate_id_exists(carrier_id, carriers_service.get_carrier_by_id, "carrier")


async def _resolve_telephony_config_id(telephony_config_id: str | None, tenant_id: str) -> None:
    if telephony_config_id is None:
        return
    cfg = await telephony_configs_service.get_telephony_config(telephony_config_id)
    # str(): a fresh row carries a UUID, a cache hit a str (see agents.py).
    if cfg is None or str(cfg.get("tenant_id")) != str(tenant_id):
        raise HTTPException(status_code=404, detail="telephony_config not found")


async def _telephony_config(telephony_config_id: str | None) -> dict | None:
    if telephony_config_id is None:
        return None
    return await telephony_configs_service.get_telephony_config(telephony_config_id, platform_scoped=True)


async def _with_provider_sync(number: dict, cfg: dict | None) -> dict:
    if cfg is None:
        return number
    sync = await number_sync.attach(cfg, number["did"])
    if sync is None:
        return number
    stored = await phone_numbers_service.record_provider_sync(number["id"], number["tenant_id"], sync)
    return {**number, "provider_sync": stored}


# 7-15 digits, "+" optional: Vobiz routes its numbers without one.
_PUBLIC_NUMBER = re.compile(r"\+?[1-9]\d{6,14}")


def _is_local_address(did: str) -> bool:
    """Inbound routing keys on the DID alone, so anything that isn't a
    public number or SIP URI is an extension on the shared Kamailio/FreeSWITCH."""
    return not (_PUBLIC_NUMBER.fullmatch(did) or did.lower().startswith("sip:"))


async def _require_superadmin_for_local_number(
    current_user: CurrentUser, did: str, telephony_config_id: str | None, carrier_id: str | None,
) -> None:
    """Local SIP numbers live on the shared Kamailio/FreeSWITCH; a tenant picking one
    could claim another tenant's extension, so only the platform assigns them."""
    if current_user.role == "superadmin":
        return
    local = _is_local_address(did) or (telephony_config_id is None and carrier_id is None)
    if not local and telephony_config_id is not None:
        # By kind, so a soft-deleted REST config's numbers stay the tenant's;
        # only a missing row fails closed.
        kind = await telephony_configs_service.get_provider_kind(telephony_config_id)
        local = kind is None or kind == telephony_configs_service.NATIVE_PROVIDER
    if local:
        raise HTTPException(
            status_code=403,
            detail="Local numbers and extensions are assigned by the platform",
        )


@tenant_scoped_router.get("")
async def list_phone_numbers(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await phone_numbers_service.list_phone_numbers(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_phone_number(
    tenant_id: str,
    body: PhoneNumberCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant_id(tenant_id)
    await _resolve_agent_id(body.agent_id)
    await _resolve_agent_id(body.fallback_agent_id)
    await _resolve_carrier_id(body.carrier_id)
    await _resolve_telephony_config_id(body.telephony_config_id, tenant_id)
    await _require_superadmin_for_local_number(current_user, body.did, body.telephony_config_id, body.carrier_id)
    cfg = await _telephony_config(body.telephony_config_id)
    if cfg is not None:
        await number_sync.ensure_owned(cfg, body.did)
    number = await phone_numbers_service.create_phone_number(
        tenant_id=tenant_id,
        did=body.did,
        agent_id=body.agent_id,
        fallback_agent_id=body.fallback_agent_id,
        carrier_id=body.carrier_id,
        telephony_config_id=body.telephony_config_id,
        region=body.region,
        status=body.status,
        user_id=current_user.id,
        user_email=current_user.email,
    )
    return await _with_provider_sync(number, cfg)


@router.get("/{phone_number_id}")
async def get_phone_number(phone_number_id: str, current_user: CurrentUser = Depends(get_current_user)):
    phone_number = await get_or_404(
        phone_numbers_service.get_phone_number(
            phone_number_id, platform_scoped=is_platform_scoped(current_user),
        ),
        f"phone_number {phone_number_id!r} not found",
    )
    await assert_tenant_access(phone_number["tenant_id"], current_user)
    return phone_number


@router.patch("/{phone_number_id}")
async def update_phone_number(
    phone_number_id: str,
    body: PhoneNumberUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    phone_number = await get_or_404(
        phone_numbers_service.get_phone_number(
            phone_number_id, platform_scoped=is_platform_scoped(current_user),
        ),
        f"phone_number {phone_number_id!r} not found",
    )
    await assert_tenant_access(phone_number["tenant_id"], current_user)
    # The resolvers below use RLS-scoped tenant_conn(), so bind the tenant first.
    set_target_tenant(phone_number["tenant_id"])
    if "agent_id" in fields:
        await _resolve_agent_id(fields["agent_id"])
    if "fallback_agent_id" in fields:
        await _resolve_agent_id(fields["fallback_agent_id"])
    if "carrier_id" in fields:
        await _resolve_carrier_id(fields["carrier_id"])
    if "telephony_config_id" in fields:
        await _resolve_telephony_config_id(fields["telephony_config_id"], phone_number["tenant_id"])
    if {"did", "telephony_config_id", "carrier_id"} & fields.keys():
        # Both where the number is now and where it would end up.
        await _require_superadmin_for_local_number(
            current_user, phone_number["did"], phone_number.get("telephony_config_id"), phone_number.get("carrier_id"),
        )
        await _require_superadmin_for_local_number(
            current_user,
            fields.get("did") or phone_number["did"],
            fields.get("telephony_config_id", phone_number.get("telephony_config_id")),
            fields.get("carrier_id", phone_number.get("carrier_id")),
        )
    def _id(value: Any) -> str | None:
        # The stored id is a UUID, the request's a str.
        return None if value is None else str(value)

    moved = ("did" in fields and fields["did"] != phone_number["did"]) or (
        "telephony_config_id" in fields
        and _id(fields["telephony_config_id"]) != _id(phone_number.get("telephony_config_id"))
    )
    old_cfg = await _telephony_config(phone_number.get("telephony_config_id")) if moved else None
    new_cfg = await _telephony_config(fields.get("telephony_config_id", phone_number.get("telephony_config_id"))) if moved else None
    if new_cfg is not None:
        await number_sync.ensure_owned(new_cfg, fields.get("did", phone_number["did"]))
    updated = await phone_numbers_service.update_phone_number(
        phone_number_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )
    if not moved:
        return updated
    if old_cfg is not None:
        released = await number_sync.detach(old_cfg, phone_number["did"])
        if released is not None and not released["ok"]:
            log.warning("phone_numbers: previous routing for %s not removed: %s", phone_number["did"], released["message"])
    return await _with_provider_sync(updated, new_cfg)


@router.post("/{phone_number_id}/sync")
async def sync_phone_number(
    phone_number_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Retry pointing one number at the platform at its provider."""
    phone_number = await get_or_404(
        phone_numbers_service.get_phone_number(
            phone_number_id, platform_scoped=is_platform_scoped(current_user),
        ),
        f"phone_number {phone_number_id!r} not found",
    )
    await assert_tenant_access(phone_number["tenant_id"], current_user)
    set_target_tenant(phone_number["tenant_id"])
    cfg = await _telephony_config(phone_number.get("telephony_config_id"))
    sync = await number_sync.attach(cfg, phone_number["did"]) if cfg is not None else None
    if sync is None:
        raise HTTPException(status_code=400, detail="this number's provider has nothing to sync")
    stored = await phone_numbers_service.record_provider_sync(phone_number["id"], phone_number["tenant_id"], sync)
    return {**phone_number, "provider_sync": stored}


@router.delete("/{phone_number_id}", status_code=204)
async def delete_phone_number(
    phone_number_id: str,
    force: bool = Query(False, description="remove it here even if the provider won't detach it"),
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    phone_number = await get_or_404(
        phone_numbers_service.get_phone_number(
            phone_number_id, platform_scoped=is_platform_scoped(current_user),
        ),
        f"phone_number {phone_number_id!r} not found",
    )
    await assert_tenant_access(phone_number["tenant_id"], current_user)
    # Releasing a local number is as platform-owned as assigning one: a tenant
    # admin who deleted it could never add it back. They can set it inactive.
    await _require_superadmin_for_local_number(
        current_user, phone_number["did"], phone_number.get("telephony_config_id"), phone_number.get("carrier_id"),
    )
    set_target_tenant(phone_number["tenant_id"])
    # Detach at the provider first: if that fails, keep the row unless the
    # admin forces it, so a number never silently keeps routing here.
    cfg = await _telephony_config(phone_number.get("telephony_config_id"))
    if cfg is not None:
        released = await number_sync.detach(cfg, phone_number["did"])
        if released is not None and not released["ok"]:
            if not force:
                raise HTTPException(status_code=502, detail=f"{cfg['provider']} didn't release the number: {released['message']}")
            log.warning("phone_numbers: %s removed without detaching at %s: %s", phone_number["did"], cfg["provider"], released["message"])
    await phone_numbers_service.soft_delete_phone_number(
        phone_number_id, user_id=current_user.id, user_email=current_user.email,
    )
