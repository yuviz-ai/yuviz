"""
Shared FastAPI dependencies — verified JWT identity, role and tenant gates.

get_current_user() enforces CONSOLE_ROLES (fencing off supervisor/agent);
get_authenticated_user() is the raw decode-or-401 for routes those roles need.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from fastapi import Depends, HTTPException, Header, Request

from libs.tenancy import set_caller_tenant, set_target_tenant

from . import tenants as tenants_service
from . import users as users_service
from .auth import CurrentUser, InvalidTokenError, decode_access_token

CONSOLE_ROLES = frozenset({"superadmin", "admin", "viewer"})

# Supervisor reaches only live-calls routes, via its own dependency, so
# CONSOLE_ROLES stays untouched.
LIVE_CALLS_ROLES = frozenset({"superadmin", "admin", "supervisor"})

# Supervisor is deliberately excluded from transcripts.
TRANSCRIPT_ROLES = frozenset({"superadmin", "admin"})

AUTHORITY_MEMO_TTL_S = 60

# Separate memo from live-calls: that one also checks role, this only existence.
CONSOLE_AUTHORITY_MEMO_TTL_S = 60

# scope_key is attacker-influenced (tenant_slug param), so cap the memo.
AUTHORITY_MEMO_MAX_ENTRIES = 10_000


async def get_authenticated_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """Decode-or-401 with no role gate."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        user = decode_access_token(token)
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    # RLS ceiling: the caller's tenant, regardless of what a path later claims.
    set_caller_tenant(user.tenant_id)
    return user


async def get_current_user(
    request: Request, user: CurrentUser = Depends(get_authenticated_user),
) -> CurrentUser:
    # JWT claims are a login-time snapshot; re-read the user row (memoized).
    user = await fresh_console_authority(request.app.state, user)
    if user.role not in CONSOLE_ROLES:
        raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot access this service")
    return user


def is_platform_scoped(user: CurrentUser) -> bool:
    """Platform scope is `tenant_id is None`, not role (also covers service accounts)."""
    return user.tenant_id is None


def require_role(*allowed_roles: str):
    """Returns a dependency that additionally rejects (403) an authenticated
    user whose role isn't in allowed_roles. Usage:
        Depends(require_role("superadmin", "admin"))
    """

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot perform this action")
        return user

    return _check


async def bind_path_tenant(request: Request) -> None:
    """Record the path tenant as the RLS target; no authorization.

    Safe before auth: current_tenant() ignores the target for tenant-scoped callers."""
    tenant = request.path_params.get("tenant_slug") or request.path_params.get("tenant_id")
    set_target_tenant(tenant)


async def require_path_tenant_access(
    request: Request, current_user: CurrentUser = Depends(get_current_user),
) -> None:
    """assert_tenant_access on the path's tenant segment; pairs with bind_path_tenant."""
    tenant = request.path_params.get("tenant_slug") or request.path_params.get("tenant_id")
    await assert_tenant_access(tenant, current_user)


async def assert_tenant_access(tenant: "str | uuid.UUID | None", current_user: CurrentUser) -> None:
    """Shared tenant-access predicate for path segments, fetched rows and body fields.

        is_platform_scoped(current_user) -> allowed
        UUID mismatch (or None) -> 403
        slug mismatch or unknown -> 404 (no slug oracle)

    Sets no GUC, so it is safe after a cache-satisfied fetch.
    """
    if is_platform_scoped(current_user):
        return
    if tenant is None:
        raise HTTPException(status_code=403, detail="tenant_id does not match the caller's tenant")
    # asyncpg rows give uuid.UUID; uuid.UUID(UUID) raises, misrouting to the slug branch.
    if isinstance(tenant, uuid.UUID):
        if str(tenant) != current_user.tenant_id:
            raise HTTPException(status_code=403, detail="tenant_id does not match the caller's tenant")
        return
    try:
        parsed = uuid.UUID(tenant)
    except (ValueError, AttributeError, TypeError):
        tenant_row = await tenants_service.get_tenant(tenant)
        if tenant_row is None or str(tenant_row["id"]) != current_user.tenant_id:
            raise HTTPException(status_code=404, detail=f"tenant {tenant!r} not found")
        return
    if str(parsed) != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="tenant_id does not match the caller's tenant")


def require_live_calls_operator():
    """Dependency admitting LIVE_CALLS_ROLES only; built on get_authenticated_user
    because supervisor must stay outside CONSOLE_ROLES."""

    async def _check(user: CurrentUser = Depends(get_authenticated_user)) -> CurrentUser:
        if user.role not in LIVE_CALLS_ROLES:
            raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot access this service")
        return user

    return _check


def _row_to_effective_user(row: dict[str, Any]) -> CurrentUser:
    """Rebuild a CurrentUser from a fresh `users` row instead of token claims."""
    return CurrentUser(
        id=str(row["id"]),
        email=row["email"],
        role=row["role"],
        tenant_id=str(row["tenant_id"]) if row["tenant_id"] is not None else None,
        is_service_account=row["is_service_account"],
        token_version=row["token_version"],
    )


def _reject_revoked(token_user: CurrentUser, effective_user: CurrentUser) -> None:
    if token_user.token_version != effective_user.token_version:
        raise HTTPException(status_code=401, detail="session expired — please sign in again")


def _fresh_hit(cached: Any, token_user: CurrentUser, now: float, ttl_s: float) -> bool:
    # A token newer than the cached row means the memo predates a password change.
    return (
        cached is not None and now - cached[0] < ttl_s
        and cached[1].token_version >= token_user.token_version
    )


def forget_user(app_state: Any, user_id: str) -> None:
    """Drops every memoized authority for this user so a password change
    revokes older tokens, and a role or tenant move takes effect,
    immediately in this process."""
    console = getattr(app_state, "_console_authority_memo", None)
    if console is not None:
        console.pop(user_id, None)
    live = getattr(app_state, "_live_calls_authority_memo", None)
    if live is not None:
        for key in [k for k in live if k[0] == user_id]:
            del live[key]


async def assert_current_authority(user: CurrentUser) -> CurrentUser:
    """Re-read the user; 403 if deleted, demoted out of LIVE_CALLS_ROLES, or re-tenanted."""
    row = await users_service.get_user_by_id(user.id)
    if row is None:
        raise HTTPException(status_code=403, detail="account is no longer active")
    if row["role"] not in LIVE_CALLS_ROLES:
        raise HTTPException(status_code=403, detail=f"role {row['role']!r} cannot access this service")
    fresh_tenant_id = str(row["tenant_id"]) if row["tenant_id"] is not None else None
    if fresh_tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="account tenant has changed; sign in again")
    effective_user = _row_to_effective_user(row)
    _reject_revoked(user, effective_user)
    return effective_user


async def fresh_console_authority(app_state: Any, user: CurrentUser) -> CurrentUser:
    """401 if the user row is gone; returns the row's current identity.

    Memoized per user.id (bounded LRU) so revocation lag is the memo TTL, not the token TTL."""
    memo: OrderedDict[str, tuple[float, CurrentUser]] = getattr(
        app_state, "_console_authority_memo", None,
    )
    if memo is None:
        memo = OrderedDict()
        app_state._console_authority_memo = memo

    now = time.monotonic()
    cached = memo.get(user.id)
    if _fresh_hit(cached, user, now, CONSOLE_AUTHORITY_MEMO_TTL_S):
        memo.move_to_end(user.id)
        _reject_revoked(user, cached[1])
        return cached[1]

    row = await users_service.get_user_by_id(user.id)
    if row is None:
        # 401, not 404: don't leak whether the id ever existed.
        raise HTTPException(status_code=401, detail="user no longer exists")

    effective_user = _row_to_effective_user(row)
    memo[user.id] = (now, effective_user)
    memo.move_to_end(user.id)
    while len(memo) > AUTHORITY_MEMO_MAX_ENTRIES:
        memo.popitem(last=False)
    _reject_revoked(user, effective_user)
    return effective_user


async def fresh_authority(
    app_state: Any, user: CurrentUser, scope_key: str, *, ttl_s: int = AUTHORITY_MEMO_TTL_S,
) -> CurrentUser:
    """Row-based identity memoized per (user.id, scope_key); 403 if deleted or demoted.

    Unlike assert_current_authority(), a changed tenant_id is returned, not rejected.
    scope_key must come from the request (tenant_slug or "self"), never the identity."""
    memo: OrderedDict[tuple[str, str], tuple[float, CurrentUser]] = getattr(
        app_state, "_live_calls_authority_memo", None,
    )
    if memo is None:
        memo = OrderedDict()
        app_state._live_calls_authority_memo = memo

    key = (user.id, scope_key)
    now = time.monotonic()
    cached = memo.get(key)
    if _fresh_hit(cached, user, now, ttl_s):
        memo.move_to_end(key)
        _reject_revoked(user, cached[1])
        return cached[1]

    row = await users_service.get_user_by_id(user.id)
    if row is None:
        raise HTTPException(status_code=403, detail="account is no longer active")
    if row["role"] not in LIVE_CALLS_ROLES:
        raise HTTPException(status_code=403, detail=f"role {row['role']!r} cannot access this service")

    effective_user = _row_to_effective_user(row)
    memo[key] = (now, effective_user)
    memo.move_to_end(key)
    while len(memo) > AUTHORITY_MEMO_MAX_ENTRIES:
        memo.popitem(last=False)
    _reject_revoked(user, effective_user)
    return effective_user


def forget_authority(app_state: Any, user_id: str, scope_key: str) -> None:
    """Evict one entry, e.g. for a nonexistent slug, so slug floods can't grow the memo."""
    memo = getattr(app_state, "_live_calls_authority_memo", None)
    if memo is not None:
        memo.pop((user_id, scope_key), None)


async def get_or_404(fetch: Awaitable[Any | None], detail: str) -> Any:
    """Await `fetch`, raising 404 with `detail` if it resolves to None."""
    row = await fetch
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


async def validate_id_exists(
    id_: str | None,
    fetch_by_id: Callable[[str], Awaitable[Any | None]],
    entity_name: str,
) -> None:
    """400 for a malformed FK id, 404 for a missing one; None is a no-op."""
    if id_ is None:
        return
    try:
        uuid.UUID(id_)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{id_!r} is not a valid {entity_name} id")
    if await fetch_by_id(id_) is None:
        raise HTTPException(status_code=404, detail=f"{entity_name} {id_!r} not found")
