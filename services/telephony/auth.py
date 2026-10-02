"""Outbound-route auth. Uses JWT-only get_authenticated_user (no Postgres on this path).
`deps.assert_tenant_access` MUST be awaited — a missing await silently removes the tenant gate."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException

from services.config import deps
from services.config.auth import CurrentUser
from services.config.deps import get_authenticated_user

from .accounts import accounts

TELEPHONY_CALLER_ROLES = frozenset({"superadmin", "admin"})


async def require_telephony_caller(
    user: CurrentUser = Depends(get_authenticated_user),
) -> CurrentUser:
    """403 unless an admin-role user or a NULL-tenant service account (which carries role="viewer")."""
    if user.role in TELEPHONY_CALLER_ROLES:
        return user
    if user.is_service_account and user.tenant_id is None:
        return user
    raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot access this service")


async def resolve_caller_tenant(user: CurrentUser, tenant_slug: str) -> tuple[uuid.UUID, str]:
    """Map tenant_slug to UUID from AccountStore. Unknown and foreign slugs both 404 (checked before
    assert_tenant_access, which would 403) so tenant-scoped callers can't probe existence."""
    tenant_id = accounts.tenant_id_for_slug(tenant_slug)
    platform_scoped = deps.is_platform_scoped(user)

    if not platform_scoped and (tenant_id is None or tenant_id != user.tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {tenant_slug!r} not found")
    if tenant_id is None:
        raise HTTPException(status_code=404, detail=f"tenant {tenant_slug!r} not found")

    tenant_uuid = uuid.UUID(tenant_id)
    await deps.assert_tenant_access(tenant_uuid, user)
    return tenant_uuid, tenant_slug
