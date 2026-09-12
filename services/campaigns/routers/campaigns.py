"""
Campaigns router — CRUD + contact CSV upload + start/pause/resume +
progress. API shape informed by Dograh's real, published campaign API
(Create/Get/List/Update Campaign, Upload Contacts CSV, Start/Pause/Resume,
Get Progress) rather than invented from scratch — see project history.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from services.config.auth import CurrentUser
from services.config.deps import get_current_user, get_or_404, require_role

from .. import campaign_contacts, campaigns as campaigns_service, dnc
from ..schemas import CampaignCreate, CampaignUpdate, DncNumberCreate

tenant_scoped_router = APIRouter(prefix="/tenants/{tenant_id}/campaigns", tags=["campaigns"])
router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@tenant_scoped_router.get("")
async def list_campaigns(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await campaigns_service.list_campaigns(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_campaign(
    tenant_id: str, body: CampaignCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    # agent_id is a NOT NULL FK server-side (database/schema.sql) but the
    # Admin UI's wizard now lets it through blank ("optional for now") — an
    # empty or foreign-tenant id must fail here with a real 422, not reach
    # the INSERT and crash with an unhandled asyncpg cast/FK error (which
    # returns without CORS headers and shows up in the browser as a bare,
    # misleading "blocked by CORS policy" fetch failure).
    if not await campaigns_service.agent_exists_for_tenant(tenant_id, body.agent_id):
        raise HTTPException(status_code=422, detail="agent_id must reference an existing agent for this tenant")
    return await campaigns_service.create_campaign(
        tenant_id, agent_id=body.agent_id, name=body.name, caller_id=body.caller_id,
        max_concurrent_calls=body.max_concurrent_calls, pacing_seconds=body.pacing_seconds,
        max_attempts=body.max_attempts,
        calling_hours_start=body.calling_hours_start, calling_hours_end=body.calling_hours_end,
        calling_hours_timezone=body.calling_hours_timezone,
        user_id=current_user.id, user_email=current_user.email,
    )


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await get_or_404(campaigns_service.get_campaign(campaign_id), f"campaign {campaign_id!r} not found")


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: str, body: CampaignUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    return await campaigns_service.update_campaign(
        campaign_id, body.model_dump(), user_id=current_user.id, user_email=current_user.email,
    )


@router.get("/{campaign_id}/progress")
async def get_progress(campaign_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await get_or_404(campaigns_service.get_campaign(campaign_id), f"campaign {campaign_id!r} not found")
    return await campaigns_service.get_progress(campaign_id)


@router.get("/{campaign_id}/contacts")
async def list_contacts(
    campaign_id: str, status: str | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
):
    await get_or_404(campaigns_service.get_campaign(campaign_id), f"campaign {campaign_id!r} not found")
    return await campaign_contacts.list_contacts(campaign_id, status=status)


@router.post("/{campaign_id}/contacts/upload", status_code=201)
async def upload_contacts(
    campaign_id: str, file: UploadFile,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    campaign = await get_or_404(campaigns_service.get_campaign(campaign_id), f"campaign {campaign_id!r} not found")
    content = await file.read()
    try:
        contacts = campaign_contacts.parse_contacts_csv(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # DNC is enforced here (not just by worker.py at dial time) so a
    # blocked number never even enters the queue as 'pending' — the
    # worker's own check is defense in depth for numbers added to the DNC
    # list after upload, not the primary enforcement point.
    blocked = {dnc.normalize_phone(row["phone_number"]) for row in await dnc.list_numbers(campaign["tenant_id"])}
    allowed = [c for c in contacts if dnc.normalize_phone(c["phone_number"]) not in blocked]
    skipped_dnc = len(contacts) - len(allowed)

    inserted = await campaign_contacts.bulk_insert_contacts(campaign_id, allowed)
    return {"inserted": inserted, "skipped_dnc": skipped_dnc}


dnc_tenant_router = APIRouter(prefix="/tenants/{tenant_id}/dnc", tags=["dnc"])


@dnc_tenant_router.get("")
async def list_dnc_numbers(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await dnc.list_numbers(tenant_id)


@dnc_tenant_router.post("", status_code=201)
async def add_dnc_number(
    tenant_id: str, body: DncNumberCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    return await dnc.add_number(tenant_id, body.phone_number, reason=body.reason)


dnc_router = APIRouter(prefix="/dnc", tags=["dnc"])


@dnc_router.delete("/{dnc_id}", status_code=204)
async def remove_dnc_number(dnc_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin"))):
    await dnc.remove_number(dnc_id)


@router.post("/{campaign_id}/start")
async def start_campaign(campaign_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin"))):
    await get_or_404(campaigns_service.get_campaign(campaign_id), f"campaign {campaign_id!r} not found")
    return await campaigns_service.set_status(
        campaign_id, "running", user_id=current_user.id, user_email=current_user.email,
    )


@router.post("/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin"))):
    await get_or_404(campaigns_service.get_campaign(campaign_id), f"campaign {campaign_id!r} not found")
    return await campaigns_service.set_status(
        campaign_id, "paused", user_id=current_user.id, user_email=current_user.email,
    )


@router.post("/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin"))):
    await get_or_404(campaigns_service.get_campaign(campaign_id), f"campaign {campaign_id!r} not found")
    return await campaigns_service.set_status(
        campaign_id, "running", user_id=current_user.id, user_email=current_user.email,
    )
