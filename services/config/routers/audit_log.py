from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import audit
from ..auth import CurrentUser
from ..deps import is_platform_scoped, require_role

router = APIRouter(prefix="/audit-log", tags=["audit-log"])


@router.get("")
async def list_audit_log(
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    user_email: str | None = Query(default=None),
    action: str | None = Query(default=None, pattern="^(created|updated|deleted)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(require_role("superadmin")),
):
    # lesson 24: role alone (`require_role("superadmin")` above) answers
    # "is this actor privileged", not "which tenant is it scoped to" — a
    # tenant-scoped superadmin must only ever see its own tenant's rows.
    platform_scoped = is_platform_scoped(current_user)
    return await audit.list_audit_log(
        tenant_id=None if platform_scoped else current_user.tenant_id,
        platform_scoped=platform_scoped,
        entity_type=entity_type,
        entity_id=entity_id,
        user_email=user_email,
        action=action,
        limit=limit,
        offset=offset,
    )
