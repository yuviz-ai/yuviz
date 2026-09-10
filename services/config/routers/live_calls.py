"""
Live Calls Monitoring — its own auth model (require_live_calls_operator),
kept out of routers/calls.py so that file's single "is authenticated"
console gate stays the one gate readers have to reason about, and this
module's tripwire (T5b) stays legible on its own.

_resolve_scope is the load-bearing function in this file: STEP 1 re-reads
the caller's current identity from the database (fresh_authority) before any
other decision, and STEP 2 branches on THAT fresh row's tenant_id — never on
the token's. See its own docstring for why branch selection is a privileged
decision too. After the fresh_authority() call below, `user` (the token) is
dead to this module: every subsequent decision reads effective_user.
"""

from __future__ import annotations

import ipaddress
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import deps
from .. import live_calls as live_calls_service
from .. import tenants as tenants_service
from ..auth import CurrentUser

router = APIRouter(prefix="/live-calls", tags=["live-calls"])


def _extract_client_ip(request: Request) -> str | None:
    """The left-most X-Forwarded-For hop, validated through
    ipaddress.ip_address() (NULL on a spoofed/malformed value — never an
    unvalidated raw header value into the audit row), falling back to
    request.client.host when no XFF header is present at all. Closes
    security finding #4."""
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

    scope_key is derived from the REQUEST only (the tenant_slug query
    parameter, or "self" when absent) — never from the caller's identity —
    so a tenant SWITCH always re-reads rather than inheriting another
    selection's validation.
    """
    scope_key = tenant_slug or "self"
    effective_user = await deps.fresh_authority(request.app.state, user, scope_key)

    # BRANCH ON effective_user.tenant_id — never on user.tenant_id. Branch
    # selection and the privilege check inside the branch derive from the
    # SAME fresh row and cannot disagree: this codebase confines by scope,
    # not by role (users.tenant_id is mutable), so trusting the token's
    # NULL/non-NULL claim here would route a just-re-tenanted superadmin
    # down the platform-scoped path for the token's remaining life.
    if effective_user.tenant_id is not None:
        tenant = await tenants_service.get_tenant_by_id(effective_user.tenant_id)
        if tenant is None or (tenant_slug is not None and tenant_slug != tenant["slug"]):
            # Identical status/body/path to a nonexistent slug — no
            # existence oracle (lesson 2). A scope_key that 404s here is
            # never useful again, so it doesn't stay in the memo either
            # (finding #8).
            deps.forget_authority(request.app.state, effective_user.id, scope_key)
            raise HTTPException(status_code=404, detail="tenant not found")
        return tenant["slug"], tenant["id"], effective_user

    if effective_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="role cannot access this service")
    if tenant_slug is None:
        raise HTTPException(status_code=400, detail="tenant_slug is required")
    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        deps.forget_authority(request.app.state, effective_user.id, scope_key)
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant["slug"], tenant["id"], effective_user


@router.get("")
async def get_live_calls(
    request: Request,
    tenant_slug: str | None = Query(default=None),
    user: CurrentUser = Depends(deps.require_live_calls_operator()),
):
    # Per-user, sized to the 5s poll — a coarse gate that can only reject,
    # never widen (like require_live_calls_operator above), so it runs
    # before the identity re-read below, keyed on the token's stable user id.
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
    # assert_current_authority — not fresh_authority — is the gate for a
    # WRITE: 403 outright on a demoted/deleted/re-tenanted actor (AC9)
    # rather than silently rescoping it the way _resolve_scope's branch
    # selection does for the read-only GET route.
    await deps.assert_current_authority(user)

    ip_address = _extract_client_ip(request)
    tenant_slug = body.tenant_slug
    scope_key = tenant_slug or "self"

    try:
        slug, tenant_id, effective_user = await _resolve_scope(request, user, tenant_slug)
    except HTTPException as exc:
        if exc.status_code == 404:
            # _resolve_scope's own branch-selection 404 was previously
            # unaudited (security finding #5) — audit it as a denial too,
            # attributed to the caller's own resolved tenant. fresh_authority
            # here is a memo hit (or a no-op re-read for a role that already
            # passed): it does not re-run tenant resolution.
            effective_user = await deps.fresh_authority(request.app.state, user, scope_key)
            if effective_user.tenant_id is not None:
                await live_calls_service.record_denied_intervention(
                    tenant_id=uuid.UUID(effective_user.tenant_id), session_id=session_id,
                    user=effective_user, ip_address=ip_address,
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
