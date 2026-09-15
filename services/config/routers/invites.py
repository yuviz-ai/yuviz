"""
Invite lifecycle — thin HTTP wrapper over invites.py, same split as
routers/users.py over users.py (CURSOR.md: routers translate HTTP <-> the
sibling module and nothing else).

The admin routes (create/list/resend/revoke) require superadmin/admin, same
as routers/users.py's write routes. The accept routes are public by
construction — no `Depends(get_current_user)` or `Depends(get_authenticated_
user)` at all — so they bypass the console gate without needing an
exemption list, and the invite token travels only in the `X-Invite-Token`
header, never a path or query segment (see invites.py / design doc's "Token
placement").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from libs.tenancy import set_target_tenant

from .. import email
from .. import invites as invites_service
from .. import users as users_service
from ..auth import CurrentUser
from ..deps import assert_tenant_access, get_or_404, is_platform_scoped, require_role
from ..schemas import InviteAccept, InviteCreate

router = APIRouter(prefix="/invites", tags=["invites"])


def _client_host(request: Request) -> str:
    # request.client is None on some transports (e.g. a Unix domain socket
    # in front of this service) — falling through to request.client.host
    # unguarded is an AttributeError -> 500, and worse, skips the throttle
    # entirely on these public, unauthenticated routes. A fixed sentinel
    # key means such requests still share one (real) rate-limit bucket
    # instead of bypassing the limiter altogether.
    return request.client.host if request.client is not None else "unknown-client"


@router.post("", status_code=201)
async def create_invite(
    body: InviteCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    # Probe cap first, outcome-blind: incremented before create_invite's own
    # users-table lookup runs, so a 409 this raises costs the actor exactly
    # what a 201 would (design doc's "Probe rate limit"). Mail-bomb cap is
    # peeked here too but only incremented after a real send below.
    request.app.state.invite_throttle.check_probe(current_user.id)
    request.app.state.invite_throttle.check_send_cap(current_user.id)

    # Tier 4: the tenant is in the request body, not a path segment. The
    # existing may_invite/_same_tenant privilege check inside create_invite
    # is kept unchanged — this runs beside it, not instead of it (lesson 24:
    # the exemption predicate is is_platform_scoped, not role=="superadmin").
    await assert_tenant_access(body.tenant_id, current_user)
    set_target_tenant(body.tenant_id)

    row, raw_token = await invites_service.create_invite(
        email=body.email, role=body.role, tenant_id=body.tenant_id, team=body.team,
        actor=current_user,
    )
    request.app.state.invite_throttle.record_send(current_user.id)

    email_sent = True
    try:
        await email.send_invite_email(to_email=row["email"], raw_token=raw_token)
    except Exception:
        # Non-fatal (AC12): the invite row is already committed pending and
        # stays resendable — a broken SMTP config must not 500 the request.
        email_sent = False

    result = invites_service.to_public_dict(row)
    result["email_sent"] = email_sent
    return result


@router.get("")
async def list_invites(
    tenant_id: str | None = None,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    # Same conflation routers/users.py had (PR #19 security finding 1):
    # is_platform_scoped (lesson 24 — tenant_id is None) answers *which
    # tenant*, not *how privileged*. require_role above keeps a NULL-tenant
    # viewer service account off this route entirely today, but a NULL-
    # tenant *admin* (not superadmin) would otherwise still fall into the
    # unscoped, cross-tenant branch on scope alone. `?tenant_id=` is
    # honored, and is_superadmin=True is passed to the service, only when
    # the actor is platform-scoped *and* actually superadmin; anyone else
    # is forced to their own tenant_id, not merely validated (AC11,
    # CURSOR.md).
    platform_scoped = is_platform_scoped(current_user) and current_user.role == "superadmin"
    scoped_tenant_id = tenant_id if platform_scoped else current_user.tenant_id
    if scoped_tenant_id is not None:
        set_target_tenant(scoped_tenant_id)
    rows = await invites_service.list_invites(
        tenant_id=scoped_tenant_id, is_superadmin=platform_scoped,
    )
    return [invites_service.to_public_dict(row) for row in rows]


async def _authorize_invite(invite_id: str, current_user: CurrentUser) -> dict:
    """Tier 3 by-id: 404 if missing, 403 if it belongs to a different
    tenant — same shared predicate as every other by-id resolver."""
    platform_scoped = is_platform_scoped(current_user)
    invite = await get_or_404(
        invites_service.get_invite_by_id(invite_id, platform_scoped=platform_scoped),
        f"invite {invite_id!r} not found",
    )
    await assert_tenant_access(invite["tenant_id"], current_user)
    return invite


@router.post("/{invite_id}/resend")
async def resend_invite(
    invite_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    request.app.state.invite_throttle.check_send_cap(current_user.id)

    invite = await _authorize_invite(invite_id, current_user)
    set_target_tenant(invite["tenant_id"])
    row, raw_token = await invites_service.resend_invite(invite_id, actor=current_user)
    request.app.state.invite_throttle.record_send(current_user.id)

    email_sent = True
    try:
        await email.send_invite_email(to_email=row["email"], raw_token=raw_token)
    except Exception:
        email_sent = False

    result = invites_service.to_public_dict(row)
    result["email_sent"] = email_sent
    return result


@router.post("/{invite_id}/revoke")
async def revoke_invite(
    invite_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    invite = await _authorize_invite(invite_id, current_user)
    set_target_tenant(invite["tenant_id"])
    row = await invites_service.revoke_invite(invite_id, actor=current_user)
    return invites_service.to_public_dict(row)


@router.get("/accept")
async def get_invite_to_accept(
    request: Request, x_invite_token: str | None = Header(default=None),
):
    # Public — no identity dependency at all, so this bypasses the console
    # gate by construction rather than needing an exemption.
    request.app.state.accept_throttle.check(_client_host(request))
    if not x_invite_token:
        raise HTTPException(status_code=404, detail="invite not found")
    return await invites_service.get_invite_for_accept(raw_token=x_invite_token)


@router.post("/accept")
async def accept_invite(
    body: InviteAccept, request: Request, x_invite_token: str | None = Header(default=None),
):
    request.app.state.accept_throttle.check(_client_host(request))
    if not x_invite_token:
        raise HTTPException(status_code=404, detail="invite not found")
    user = await invites_service.accept_invite(raw_token=x_invite_token, password=body.password)
    return users_service.to_public_dict(user)
