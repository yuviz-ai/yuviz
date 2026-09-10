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

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import deps
from .. import live_calls as live_calls_service
from .. import tenants as tenants_service
from ..auth import CurrentUser

router = APIRouter(prefix="/live-calls", tags=["live-calls"])


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
            # existence oracle (lesson 2).
            raise HTTPException(status_code=404, detail="tenant not found")
        return tenant["slug"], tenant["id"], effective_user

    if effective_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="role cannot access this service")
    if tenant_slug is None:
        raise HTTPException(status_code=400, detail="tenant_slug is required")
    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant["slug"], tenant["id"], effective_user


@router.get("")
async def get_live_calls(
    request: Request,
    tenant_slug: str | None = Query(default=None),
    user: CurrentUser = Depends(deps.require_live_calls_operator()),
):
    slug, _tenant_id, effective_user = await _resolve_scope(request, user, tenant_slug)
    return await live_calls_service.get_live_calls(
        slug, include_transcript=effective_user.role in deps.TRANSCRIPT_ROLES,
    )
