"""Invite lifecycle routes over invites.py.

Admin routes require superadmin/admin. Accept routes are public (no identity dependency);
the token travels only in the `X-Invite-Token` header, never the URL.
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
    # request.client can be None (e.g. Unix socket); share one throttle bucket rather than bypass it.
    return request.client.host if request.client is not None else "unknown-client"


@router.post("", status_code=201)
async def create_invite(
    body: InviteCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    # Probe cap counts before the lookup so a 409 costs the same as a 201; send cap counts only real sends.
    request.app.state.invite_throttle.check_probe(current_user.id)
    request.app.state.invite_throttle.check_send_cap(current_user.id)

    # Tenant comes from the body; create_invite still runs its own privilege check.
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
        # Non-fatal: the invite is committed and stays resendable.
        email_sent = False

    result = invites_service.to_public_dict(row)
    result["email_sent"] = email_sent
    return result


@router.get("")
async def list_invites(
    tenant_id: str | None = None,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    # `?tenant_id=` is honored only for a platform-scoped superadmin; a NULL-tenant admin
    # must not get the cross-tenant branch. Everyone else is forced to their own tenant.
    platform_scoped = is_platform_scoped(current_user) and current_user.role == "superadmin"
    scoped_tenant_id = tenant_id if platform_scoped else current_user.tenant_id
    if scoped_tenant_id is not None:
        set_target_tenant(scoped_tenant_id)
    rows = await invites_service.list_invites(
        tenant_id=scoped_tenant_id, is_superadmin=platform_scoped,
    )
    return [invites_service.to_public_dict(row) for row in rows]


async def _authorize_invite(invite_id: str, current_user: CurrentUser) -> dict:
    """404 if missing, 403 if it belongs to a different tenant."""
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
