"""
Outbound-route authentication/authorization — see 02-design.md's `auth.py`
section for the full rationale, including why `deps.assert_tenant_access`
MUST be awaited (a missing `await` silently deletes the entire tenant
gate) and why `get_authenticated_user` (pure JWT decode) is the deliberate
choice over `get_current_user` (which reads Postgres, forbidden on this
Redis-only path — lesson 27's accepted revocation-lag trade-off).
"""

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
    """401 on a missing/invalid/expired Bearer token (get_authenticated_user
    raises it itself). 403 unless the caller is either a
    TELEPHONY_CALLER_ROLES console user, or a platform service account —
    "is this actor privileged?" and "which tenant is this actor scoped to?"
    are different questions (lesson 24): a tenant-scoped viewer fails both
    clauses, the NULL-tenant service account (Campaigns/Conversation, which
    both carry role="viewer") passes the second."""
    if user.role in TELEPHONY_CALLER_ROLES:
        return user
    if user.is_service_account and user.tenant_id is None:
        return user
    raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot access this service")


async def resolve_caller_tenant(user: CurrentUser, tenant_slug: str) -> tuple[uuid.UUID, str]:
    """Maps tenant_slug -> tenant UUID via the AccountStore's already-loaded
    map (no Postgres, no tenants.get_tenant slug lookup — see the Latency
    section's tightening #1). A tenant-scoped caller must see an identical
    404 whether the slug is unmapped or belongs to another tenant (lesson
    2: per-caller invariance) — that comparison is made here, BEFORE the
    resolved UUID ever reaches deps.assert_tenant_access, because its own
    UUID branch raises 403 on a mismatch, not 404, which would let a
    tenant-scoped caller distinguish "doesn't exist" from "not mine" by
    status code alone. assert_tenant_access is still AWAITED unconditionally
    on every surviving path — a no-op confirmation for the case this
    function has already admitted, not a second, differently-shaped guard."""
    tenant_id = accounts.tenant_id_for_slug(tenant_slug)
    platform_scoped = deps.is_platform_scoped(user)

    if not platform_scoped and (tenant_id is None or tenant_id != user.tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {tenant_slug!r} not found")
    if tenant_id is None:
        raise HTTPException(status_code=404, detail=f"tenant {tenant_slug!r} not found")

    tenant_uuid = uuid.UUID(tenant_id)
    await deps.assert_tenant_access(tenant_uuid, user)
    return tenant_uuid, tenant_slug
