from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import calls as calls_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404

tenant_scoped_router = APIRouter(prefix="/tenants/{tenant_slug}/calls", tags=["calls"])
router = APIRouter(prefix="/calls", tags=["calls"])


async def _caller_tenant_slug(current_user: CurrentUser) -> str | None:
    """None = platform-scoped (superadmin / NULL-tenant service account).

    calls.tenant_id is a TEXT slug, so tenant-scoped actors resolve JWT
    tenant_id (UUID) → slug before the query predicate.
    """
    is_unscoped = current_user.role == "superadmin" or current_user.tenant_id is None
    if is_unscoped:
        return None
    tenant = await tenants_service.get_tenant_by_id(current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=403, detail="caller tenant not found")
    return tenant["slug"]


@tenant_scoped_router.get("")
async def list_calls(
    tenant_slug: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    direction: str | None = Query(default=None, pattern="^(inbound|outbound)$"),
    current_user: CurrentUser = Depends(get_current_user),
):
    await get_or_404(tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found")
    return await calls_service.list_calls(tenant_slug, limit=limit, offset=offset, direction=direction)


@tenant_scoped_router.get("/latency-stats")
async def get_latency_stats(
    tenant_slug: str,
    hours: int = Query(default=24, ge=1, le=720),
    current_user: CurrentUser = Depends(get_current_user),
):
    await get_or_404(tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found")
    return await calls_service.get_latency_stats(tenant_slug, hours=hours)


@tenant_scoped_router.get("/dashboard-stats")
async def get_dashboard_stats(
    tenant_slug: str,
    hours: int = Query(default=24 * 30, ge=1, le=24 * 365),
    current_user: CurrentUser = Depends(get_current_user),
):
    await get_or_404(tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found")
    return await calls_service.get_dashboard_stats(tenant_slug, hours=hours)


@tenant_scoped_router.get("/usage-trend")
async def get_usage_trend(
    tenant_slug: str,
    days: int = Query(default=30, ge=1, le=365),
    current_user: CurrentUser = Depends(get_current_user),
):
    await get_or_404(tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found")
    return await calls_service.get_usage_trend(tenant_slug, days=days)


@tenant_scoped_router.get("/todays-activity")
async def get_todays_activity(tenant_slug: str, current_user: CurrentUser = Depends(get_current_user)):
    await get_or_404(tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found")
    return await calls_service.get_todays_activity(tenant_slug)


@router.get("/{session_id}")
async def get_call(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    tenant_slug = await _caller_tenant_slug(current_user)
    return await get_or_404(
        calls_service.get_call(session_id, tenant_slug=tenant_slug),
        f"call {session_id!r} not found",
    )


@router.get("/{session_id}/transcript")
async def get_transcript(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    tenant_slug = await _caller_tenant_slug(current_user)
    await get_or_404(
        calls_service.get_call(session_id, tenant_slug=tenant_slug),
        f"call {session_id!r} not found",
    )
    return await calls_service.get_transcript(session_id, tenant_slug=tenant_slug)
