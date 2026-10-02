"""Live Calls Monitoring routes, with their own operator auth model.

Scope decisions use the identity freshly re-read from the database, never the token's claims.
"""

from __future__ import annotations

import ipaddress
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from libs.tenancy import set_target_tenant

from .. import deps
from .. import live_calls as live_calls_service
from .. import tenants as tenants_service
from ..auth import CurrentUser

router = APIRouter(prefix="/live-calls", tags=["live-calls"])


def _extract_client_ip(request: Request) -> str | None:
    """Left-most X-Forwarded-For hop if it's a valid IP (None if malformed), else request.client.host."""
    xff = request.headers.get("x-forwarded-for")
    if xff is not None:
        candidate = xff.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return None
        return candidate
    return request.client.host if request.client is not None else None


async def _resolve_scope(
    request: Request, user: CurrentUser, tenant_slug: str | None,
) -> tuple[str, uuid.UUID, CurrentUser]:
    """Returns (slug, tenant_uuid, effective_user) or raises.

    scope_key comes from the request only, so switching tenants always re-reads authority.
    """
    scope_key = tenant_slug or "self"
    effective_user = await deps.fresh_authority(request.app.state, user, scope_key)

    # Branch on the fresh row's tenant_id, never the token's: users.tenant_id is mutable.
    if effective_user.tenant_id is not None:
        tenant = await tenants_service.get_tenant_by_id(effective_user.tenant_id)
        if tenant is None:
            # Actor's own tenant was deleted: 403, and never fall through to the platform-scoped branch.
            deps.forget_authority(request.app.state, effective_user.id, scope_key)
            raise HTTPException(status_code=403, detail="account tenant is no longer active")
        if tenant_slug is not None and tenant_slug != tenant["slug"]:
            # Same 404 as a nonexistent slug, so this isn't an existence oracle.
            deps.forget_authority(request.app.state, effective_user.id, scope_key)
            raise HTTPException(status_code=404, detail="tenant not found")
        set_target_tenant(str(tenant["id"]))
        return tenant["slug"], tenant["id"], effective_user

    if effective_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="role cannot access this service")
    if tenant_slug is None:
        raise HTTPException(status_code=400, detail="tenant_slug is required")
    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        deps.forget_authority(request.app.state, effective_user.id, scope_key)
        raise HTTPException(status_code=404, detail="tenant not found")
    set_target_tenant(str(tenant["id"]))
    return tenant["slug"], tenant["id"], effective_user


@router.get("")
async def get_live_calls(
    request: Request,
    tenant_slug: str | None = Query(default=None),
    user: CurrentUser = Depends(deps.require_live_calls_operator()),
):
    # Per-user throttle sized to the 5s poll; it can only reject, so it runs before the re-read.
    request.app.state.live_calls_throttle.check(user.id)
    slug, _tenant_id, effective_user = await _resolve_scope(request, user, tenant_slug)
    return await live_calls_service.get_live_calls(
        slug, include_transcript=effective_user.role in deps.TRANSCRIPT_ROLES,
    )


@router.post("/{session_id}/interventions", status_code=202)
async def request_intervention(
    session_id: str,
    body: live_calls_service.InterventionRequest,
    request: Request,
    user: CurrentUser = Depends(deps.require_live_calls_operator()),
):
    # Writes 403 on a demoted/deleted/re-tenanted actor instead of silently rescoping.
    fresh = await deps.assert_current_authority(user)

    ip_address = _extract_client_ip(request)
    tenant_slug = body.tenant_slug

    try:
        slug, tenant_id, effective_user = await _resolve_scope(request, user, tenant_slug)
        if (effective_user.tenant_id, effective_user.role) != (fresh.tenant_id, fresh.role):
            # Stale memo entry (another process may have changed the user): drop it and re-resolve.
            deps.forget_user(request.app.state, user.id)
            slug, tenant_id, effective_user = await _resolve_scope(request, user, tenant_slug)
            if (effective_user.tenant_id, effective_user.role) != (fresh.tenant_id, fresh.role):
                raise HTTPException(status_code=403, detail="account tenant has changed; sign in again")
    except HTTPException as exc:
        if exc.status_code == 404:
            # Audit _resolve_scope's 404 as a denial under the tenant just read
            # from the DB, never a memo entry that may predate a re-tenant.
            if fresh.tenant_id is not None:
                await live_calls_service.record_denied_intervention(
                    tenant_id=uuid.UUID(fresh.tenant_id), session_id=session_id,
                    user=fresh, ip_address=ip_address,
                )
        raise

    result = await live_calls_service.request_intervention(
        tenant_slug=slug, tenant_id=tenant_id, session_id=session_id, action=body.action,
        user=effective_user, ip_address=ip_address,
    )
    if result is None:
        await live_calls_service.record_denied_intervention(
            tenant_id=tenant_id, session_id=session_id, user=effective_user, ip_address=ip_address,
        )
        raise HTTPException(status_code=404, detail="call not found")
    return result
