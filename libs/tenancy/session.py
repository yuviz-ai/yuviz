"""libs/tenancy/session.py — the shared RLS scope reader and the two
connection helpers every service opens Postgres through.

Framework-free: no FastAPI, no service-specific import. The six services
must not import each other's db.py (each db.py docstring states it owns
only its own pool lifecycle) — this module is the one thing they all share
instead.

Two questions, kept separate on purpose (design "Approach"):
  - `caller`: which tenant the AUTHENTICATED ACTOR belongs to (None = a
    platform-scoped account, tenant_id IS NULL).
  - `target`: which tenant the REQUEST'S OWN PATH/QUERY/BODY names.

`current_tenant()` is `target if caller is None else caller` — the target is
honoured only for an actor who has no tenant of their own, which is what
makes the database policy an independent second layer rather than a mirror
of whatever the URL says.
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
    """Raised before yielding a connection whenever no tenant could be
    resolved for it — never as a bare unset GUC that silently reads back as
    "no data" (AC 9)."""


class TenantScopeConflict(RuntimeError):
    """Raised when an `explicit_tenant` passed to tenant_conn() disagrees
    with the request's own caller scope. The caller always wins; an
    override is only usable where there is genuinely no caller scope."""


@dataclass(frozen=True)
class TenantScope:
    caller: str | None = None   # tenant UUID from the JWT; None = platform-scoped actor
    target: str | None = None   # tenant UUID or slug taken from the request's own path/query


_scope: ContextVar[TenantScope] = ContextVar("_scope", default=TenantScope())


def set_caller_tenant(tenant_id: str | None) -> None:
    """Replaces `.caller`, keeps `.target`. Called once, from
    deps.get_authenticated_user, right after JWT decode."""
    _scope.set(replace(_scope.get(), caller=tenant_id))


def set_target_tenant(tenant: str | None) -> None:
    """Replaces `.target`, keeps `.caller`. `tenant` may be a UUID or a
    slug — the raw string is stored as-is; tenant_conn() decides which GUC
    slot it belongs in when it resolves the connection."""
    _scope.set(replace(_scope.get(), target=tenant))


def current_scope() -> TenantScope:
    return _scope.get()


def current_tenant() -> str | None:
    """`target if caller is None else caller`.

    A caller who has a tenant of their own is pinned to it; the path/query
    target is honoured only for a genuinely platform-scoped actor
    (`tenant_id IS NULL`, deps.is_platform_scoped — lesson 24). This is what
    makes RLS an independent second layer instead of a mirror of the URL.
    """
    scope = _scope.get()
    return scope.target if scope.caller is None else scope.caller


def _split_tenant(tenant: "str | uuid.UUID | None") -> tuple[str | None, str | None]:
    """A resolved tenant value may be a UUID or a slug. Disambiguate by
    shape: a value that parses as a UUID fills the id slot, anything else
    fills the slug slot — never both, so the resolver's ::uuid cast never
    sees a non-UUID string.

    Accepts an actual `uuid.UUID` instance as well as a string: asyncpg
    returns UUID-typed columns (e.g. `row["tenant_id"]`) as `uuid.UUID`
    objects, and `uuid.UUID(existing_uuid_obj)` raises `AttributeError`
    (it expects a hex string) rather than round-tripping — every such
    caller was silently misrouted into the slug branch, and unresolvable
    down that branch. Found while auditing callers passing `tenant_id`
    straight from a DB row into `stamp_tenant=`/`explicit_tenant=`."""
    if tenant is None:
        return None, None
    if isinstance(tenant, uuid.UUID):
        return str(tenant), None
    try:
        return str(uuid.UUID(tenant)), None
    except (ValueError, AttributeError, TypeError):
        return None, tenant


# One statement: resolves both GUCs from `tenants` in a single round trip, so
# a caller holding only a slug (Conversation always does) and a caller
# holding only a UUID both work, and no table's policy needs a subquery.
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
    """Acquires a pooled connection, opens a transaction, resolves both
    tenant GUCs via `SET LOCAL` (reverted automatically at transaction end —
    AC 8, safe on a reused pooled connection), then yields the connection.

    `explicit_tenant` is for the enumerated sites with no request context
    (Conversation, the ingestion worker) — every other call site leaves it
    unset and is scoped by the ambient ContextVar via current_tenant().
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
        # Rollout step 2 (shadow verification, design "Migration and
        # rollout"): two observability lines with no behavioural effect,
        # logged unconditionally rather than behind a staging-only flag
        # (this repo has none) — the operator reads them from whichever
        # environment they're watching.
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
    """Acquires a pooled connection, opens a transaction, and issues
    `SET LOCAL ROLE yuviz_platform` — a Postgres role attribute
    (`BYPASSRLS`), not an application assertion any policy has to believe.
    Reverts at transaction end exactly like `SET LOCAL`.

    `reason` is mandatory so every bypass is self-documenting and
    greppable. `stamp_tenant`, when the mutation is on behalf of a known
    tenant, sets the same GUCs `tenant_conn` would — under BYPASSRLS they
    restrict nothing, so their only consumer is `audit_log.tenant_id`'s
    column default.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE yuviz_platform")
            if stamp_tenant is not None:
                await _resolve_scope_on(conn, stamp_tenant)
            log.info("platform_conn bypass reason=%s", reason)
            yield conn
