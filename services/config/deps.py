"""
Shared FastAPI dependencies — real request-scoped identity.

get_current_user() verifies a signed JWT (see auth.py) and returns the
CurrentUser it encodes; it raises 401 itself rather than returning None, so
a route depending on it is unreachable without a valid token — there is no
"forgot to check" failure mode the way an optional dependency would have.
This replaces the previous current_user_email, which only read a
caller-supplied header with no verification at all — real, but never
enforced, identity.

require_role() builds on top of it for endpoints that need more than "any
authenticated user" (e.g. only superadmin/admin may write, viewer is
read-only).

CONSOLE_ROLES / get_current_user() also fence off `supervisor`/`agent` — two
roles added to `users_role_check` for the invite feature that have no Config
API surface at all in this build (see design doc's "the console-role gate").
`get_authenticated_user` is the raw decode-or-401 step, unchanged from
before; it exists as its own name only for `/auth/me` and
`/auth/change-password`, which a supervisor/agent must still be able to
reach. Every other route keeps depending on `get_current_user`, so the gate
sits inside identity resolution itself rather than being an exemption list
some future router can forget to add itself to.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Awaitable, Callable

from fastapi import Depends, HTTPException, Header

from . import users as users_service
from .auth import CurrentUser, InvalidTokenError, decode_access_token

CONSOLE_ROLES = frozenset({"superadmin", "admin", "viewer"})

# Live Calls Monitoring: supervisor reaches exactly these two routes, and no
# others — see require_live_calls_operator below, built on
# get_authenticated_user rather than get_current_user so CONSOLE_ROLES stays
# untouched (lesson 4).
LIVE_CALLS_ROLES = frozenset({"superadmin", "admin", "supervisor"})

# Matches who reaches GET /calls/{id}/transcript today (get_current_user's
# CONSOLE_ROLES minus viewer — see routers/calls.py) — supervisor is
# deliberately excluded (AC15).
TRANSCRIPT_ROLES = frozenset({"superadmin", "admin"})

AUTHORITY_MEMO_TTL_S = 60


async def get_authenticated_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """Decode-or-401. No role gate — see module docstring for why this name
    exists separately from get_current_user()."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return decode_access_token(token)
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid or expired token")


async def get_current_user(user: CurrentUser = Depends(get_authenticated_user)) -> CurrentUser:
    if user.role not in CONSOLE_ROLES:
        raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot access this service")
    return user


def is_platform_scoped(user: CurrentUser) -> bool:
    """"Is this actor privileged?" and "which tenant is this actor scoped
    to?" are different questions (lesson 24) — the scoping one is answered
    by `tenant_id is None`, not by `role == "superadmin"`. A NULL tenant_id
    also covers the viewer-role service accounts (Conversation's startup
    prewarm, vobiz's per-call telephony lookup), which legitimately need
    platform-wide reads and are not superadmins. Routes that gate a
    `?tenant_id=` filter or an unscoped listing on "is this actor
    platform-scoped" should call this, not compare role directly — see
    routers/tenants.py's list_tenants for the original correct version of
    this predicate."""
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


def require_live_calls_operator():
    """Returns a dependency admitting exactly superadmin/admin/supervisor —
    403 for viewer/agent and any future role. Built on get_authenticated_user,
    NOT get_current_user: supervisor is deliberately outside CONSOLE_ROLES and
    must stay outside it (lesson 4), so this grant lives on its own dependency
    rather than widening the shared console gate."""

    async def _check(user: CurrentUser = Depends(get_authenticated_user)) -> CurrentUser:
        if user.role not in LIVE_CALLS_ROLES:
            raise HTTPException(status_code=403, detail=f"role {user.role!r} cannot access this service")
        return user

    return _check


def _row_to_effective_user(row: dict[str, Any]) -> CurrentUser:
    """Rebuild a CurrentUser from a fresh `users` row rather than a token's
    claims — the row is what fresh_authority()/assert_current_authority()
    exist to substitute for `user.tenant_id`/`user.role` everywhere after
    their call (lesson 35: a JWT claim is a login-time snapshot, not a live
    fact)."""
    return CurrentUser(
        id=str(row["id"]),
        email=row["email"],
        role=row["role"],
        tenant_id=str(row["tenant_id"]) if row["tenant_id"] is not None else None,
        is_service_account=row["is_service_account"],
    )


async def assert_current_authority(user: CurrentUser) -> CurrentUser:
    """Re-read users WHERE id=$1 AND deleted_at IS NULL. 403 if the row is
    gone, if role has changed out of LIVE_CALLS_ROLES, or if tenant_id no
    longer matches the token's. Closes lesson 27 / AC9 for the intervention
    route, which must refuse a demoted, deleted or re-tenanted actor outright
    rather than silently rescoping it the way fresh_authority()'s branch
    selection does."""
    row = await users_service.get_user_by_id(user.id)
    if row is None:
        raise HTTPException(status_code=403, detail="account is no longer active")
    if row["role"] not in LIVE_CALLS_ROLES:
        raise HTTPException(status_code=403, detail=f"role {row['role']!r} cannot access this service")
    fresh_tenant_id = str(row["tenant_id"]) if row["tenant_id"] is not None else None
    if fresh_tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="account tenant has changed; sign in again")
    return _row_to_effective_user(row)


async def fresh_authority(
    app_state: Any, user: CurrentUser, scope_key: str, *, ttl_s: int = AUTHORITY_MEMO_TTL_S,
) -> CurrentUser:
    """The same `users` re-read as assert_current_authority(), returning a
    CurrentUser built from the ROW's current role/tenant_id rather than the
    token's claims — memoized per (user.id, scope_key) for ttl_s in an
    in-process dict on app_state (same placement convention as
    app.state.invite_throttle, services/config/app.py:250).

    Unlike assert_current_authority(), this does NOT reject a tenant_id that
    no longer matches the token's claim — _resolve_scope's branch selection
    is exactly what needs the row's current tenant_id, not a rejection of it.
    It still 403s on a gone row or a role that has left LIVE_CALLS_ROLES.

    scope_key must come from the REQUEST (the tenant_slug query parameter, or
    "self" when absent), never from the caller's identity, so a tenant SWITCH
    always re-reads instead of inheriting another selection's validation."""
    memo: dict[tuple[str, str], tuple[float, CurrentUser]] = getattr(
        app_state, "_live_calls_authority_memo", None,
    )
    if memo is None:
        memo = {}
        app_state._live_calls_authority_memo = memo

    key = (user.id, scope_key)
    now = time.monotonic()
    cached = memo.get(key)
    if cached is not None and now - cached[0] < ttl_s:
        return cached[1]

    row = await users_service.get_user_by_id(user.id)
    if row is None:
        raise HTTPException(status_code=403, detail="account is no longer active")
    if row["role"] not in LIVE_CALLS_ROLES:
        raise HTTPException(status_code=403, detail=f"role {row['role']!r} cannot access this service")

    effective_user = _row_to_effective_user(row)
    memo[key] = (now, effective_user)
    return effective_user


async def get_or_404(fetch: Awaitable[Any | None], detail: str) -> Any:
    """Await `fetch`, raising a clean 404 with `detail` if it resolves to
    None, instead of letting the caller repeat the same if-None-raise
    block. Same fetch-then-check shape used to be inlined separately in
    each router (tenants, agents, calls, phone_numbers, provider_configs)."""
    row = await fetch
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


async def validate_id_exists(
    id_: str | None,
    fetch_by_id: Callable[[str], Awaitable[Any | None]],
    entity_name: str,
) -> None:
    """UUID-format-then-existence check for a foreign-key id in a request
    body, e.g. phone_numbers.agent_id — a clean 400 (malformed id) or 404
    (well-formed but nonexistent) instead of an INSERT's FK violation
    reaching the client as a raw 500. A None id_ (optional FK, e.g. no
    fallback_agent_id given) is a no-op, not an error."""
    if id_ is None:
        return
    try:
        uuid.UUID(id_)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{id_!r} is not a valid {entity_name} id")
    if await fetch_by_id(id_) is None:
        raise HTTPException(status_code=404, detail=f"{entity_name} {id_!r} not found")
