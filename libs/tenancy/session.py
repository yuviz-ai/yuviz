"""RLS tenant scope and the connection helpers every service opens Postgres through.

`caller` is the authenticated actor's tenant; `target` is what the request names.
Target is honoured only for platform-scoped actors, so RLS never just mirrors the URL.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import AsyncIterator

import asyncpg

log = logging.getLogger(__name__)


class TenantUnresolved(RuntimeError):
    """No tenant resolved; raised instead of an unset GUC that silently reads as "no data"."""


class TenantScopeConflict(RuntimeError):
    """explicit_tenant disagrees with the caller scope; the caller always wins."""


@dataclass(frozen=True)
class TenantScope:
    caller: str | None = None   # tenant UUID from the JWT; None = platform-scoped actor
    target: str | None = None   # tenant UUID or slug taken from the request's own path/query


_scope: ContextVar[TenantScope] = ContextVar("_scope", default=TenantScope())


def set_caller_tenant(tenant_id: str | None) -> None:
    """Replaces `.caller`, keeps `.target`. Set right after JWT decode."""
    _scope.set(replace(_scope.get(), caller=tenant_id))


def set_target_tenant(tenant: str | None) -> None:
    """Replaces `.target`, keeps `.caller`. `tenant` may be a UUID or a slug."""
    _scope.set(replace(_scope.get(), target=tenant))


def current_scope() -> TenantScope:
    return _scope.get()


def current_tenant() -> str | None:
    """`target if caller is None else caller`: tenant-scoped actors are pinned to their own tenant."""
    scope = _scope.get()
    return scope.target if scope.caller is None else scope.caller


def _split_tenant(tenant: "str | uuid.UUID | None") -> tuple[str | None, str | None]:
    """(id, slug): UUID-shaped values (str or uuid.UUID) fill the id slot, else the slug slot."""
    if tenant is None:
        return None, None
    if isinstance(tenant, uuid.UUID):
        return str(tenant), None
    try:
        return str(uuid.UUID(tenant)), None
    except (ValueError, AttributeError, TypeError):
        return None, tenant


# Sets both GUCs in one round trip from either a UUID or a slug.
_RESOLVE_SQL = """
    SELECT set_config('app.tenant_id',   t.id::text, true),
           set_config('app.tenant_slug', t.slug,     true)
      FROM tenants t
     WHERE ($1::uuid IS NOT NULL AND t.id = $1::uuid)
        OR ($1::uuid IS NULL AND $2::text IS NOT NULL AND t.slug = $2)
"""


async def _resolve_scope_on(conn: asyncpg.Connection, tenant: str | None) -> None:
    tenant_id, tenant_slug = _split_tenant(tenant)
    row = await conn.fetchrow(_RESOLVE_SQL, tenant_id, tenant_slug)
    if row is None:
        raise TenantUnresolved(f"could not resolve a tenant for this connection (tenant={tenant!r})")


@asynccontextmanager
async def tenant_conn(
    pool: asyncpg.Pool,
    *,
    explicit_tenant: str | None = None,  # UUID *or* slug; only for the enumerated no-request-context sites
    reason: str | None = None,           # mandatory whenever explicit_tenant is given
) -> AsyncIterator[asyncpg.Connection]:
    """Yield a pooled connection in a transaction with tenant GUCs set via SET LOCAL.

    `explicit_tenant` is only for sites with no request context; others use current_tenant().
    """
    if explicit_tenant is not None and reason is None:
        raise ValueError("reason= is mandatory whenever explicit_tenant is given")

    scope = current_scope()
    if explicit_tenant is not None:
        if scope.caller is not None and str(explicit_tenant) != str(scope.caller):
            raise TenantScopeConflict(
                f"explicit_tenant={explicit_tenant!r} conflicts with caller tenant {scope.caller!r}"
            )
        tenant = explicit_tenant
    else:
        tenant = current_tenant()
        if tenant is None:
            log.warning(
                "tenant_conn: unresolved scope (caller=%r, target=%r) — either a "
                "conversion this rollout missed, or a request with genuinely no "
                "tenant; about to raise TenantUnresolved",
                scope.caller, scope.target,
            )
        elif scope.caller is not None and scope.target is not None and str(scope.target) != str(scope.caller):
            log.warning(
                "tenant_conn: cross-tenant admin path (caller=%r, target=%r) — "
                "current_tenant() pins to caller; this is the account class the "
                "pre-cutover sweep must have already reviewed",
                scope.caller, scope.target,
            )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _resolve_scope_on(conn, tenant)
            yield conn


@asynccontextmanager
async def platform_conn(
    pool: asyncpg.Pool,
    *,
    reason: str,
    stamp_tenant: str | None = None,  # UUID or slug; sets the GUCs for audit_log's DEFAULT only
) -> AsyncIterator[asyncpg.Connection]:
    """Yield a connection under SET LOCAL ROLE yuviz_platform (BYPASSRLS).

    `reason` makes every bypass greppable; `stamp_tenant` only feeds audit_log's default.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE yuviz_platform")
            if stamp_tenant is not None:
                await _resolve_scope_on(conn, stamp_tenant)
            log.info("platform_conn bypass reason=%s", reason)
            yield conn
