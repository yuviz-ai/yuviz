"""User CRUD + authentication. Uncached so role changes take effect immediately.
bcrypt calls always go through asyncio.to_thread (~250ms CPU each)."""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, auth, db

_UPDATABLE_FIELDS = {"role", "tenant_id"}

_SUPERADMIN_EXISTS = (
    "SELECT EXISTS (SELECT 1 FROM users WHERE role = 'superadmin' AND deleted_at IS NULL)"
)

_BOOTSTRAP_LOCK_KEY = 7749012026


def to_public_dict(user: dict[str, Any]) -> dict[str, Any]:
    """Strips password_hash, which is never returned to a client."""
    return {k: v for k, v in user.items() if k != "password_hash"}


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Pre-auth (login): there is no tenant to scope this read to yet, and
    the row may be a NULL-tenant platform account — platform_conn bypass."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-login") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE lower(email) = lower($1) AND deleted_at IS NULL", email,
        )
    return dict(row) if row is not None else None


async def get_user_by_id(user_id: Any) -> dict[str, Any] | None:
    """Identity resolution only ("who is the actor"); bypasses RLS to see NULL-tenant
    accounts. Use get_user_for_admin() for authorization lookups."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="identity-resolution") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL", user_id,
        )
    return dict(row) if row is not None else None


async def get_user_for_admin(user_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """By-id lookup for PATCH/DELETE /users; tenant-scoped unless `platform_scoped`
    (from deps.is_platform_scoped only), so cross-tenant ids come back None."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="users-admin-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL", user_id,
        )
    return dict(row) if row is not None else None


async def list_users(*, tenant_id: Any | None, is_platform_scoped: bool) -> list[dict[str, Any]]:
    # Service accounts are hidden: deleting one via the UI breaks live calls.
    # `is_platform_scoped` is the actor's tenant scope, not role; only tenant-scoped
    # actors get the superadmin exclusion. NULL tenant_id needs platform_conn (no RLS match).
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="users-null-tenant-listing") if tenant_id is None else tenant_conn(pool)
    async with conn_cm as conn:
        if is_platform_scoped and tenant_id is None:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE deleted_at IS NULL AND NOT is_service_account ORDER BY email",
            )
        elif is_platform_scoped:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE tenant_id IS NOT DISTINCT FROM $1 "
                "AND deleted_at IS NULL AND NOT is_service_account ORDER BY email",
                tenant_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE tenant_id IS NOT DISTINCT FROM $1 "
                "AND role != 'superadmin' AND deleted_at IS NULL AND NOT is_service_account ORDER BY email",
                tenant_id,
            )
    return [dict(row) for row in rows]


async def _insert_user(
    conn: Any,
    *,
    email: str,
    password: str | None = None,
    password_hash: str | None = None,
    role: str,
    tenant_id: Any | None,
    creator_user_id: Any | None,
    creator_user_email: str | None,
    team: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    signup_source: str | None = None,
    password_set: bool = True,
) -> dict[str, Any]:
    """Insert + audit on the caller's transaction. Pass exactly one of
    password / password_hash (already bcrypted)."""
    if password_hash is None:
        password_hash = await asyncio.to_thread(auth.hash_password, password)
    row = await conn.fetchrow(
        "INSERT INTO users (email, password_hash, role, tenant_id, team, "
        "first_name, last_name, phone, signup_source, password_set) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING *",
        email.lower(), password_hash, role, tenant_id, team,
        first_name, last_name, phone, signup_source, password_set,
    )
    result = dict(row)
    await audit.write_audit(
        conn,
        entity_type="user",
        entity_id=result["id"],
        action="created",
        user_id=creator_user_id,
        user_email=creator_user_email,
        new_value=result,
    )
    return result


async def create_user(
    *,
    email: str,
    password: str,
    role: str = "admin",
    tenant_id: Any | None = None,
    creator_user_id: Any | None = None,
    creator_user_email: str | None = None,
) -> dict[str, Any]:
    # No request-scope tenant to inherit here (tenant_id may be NULL), so bypass RLS.
    pool = await db.get_pool()
    async with platform_conn(pool, reason="users-create", stamp_tenant=tenant_id) as conn:
        return await _insert_user(
            conn,
            email=email,
            password=password,
            role=role,
            tenant_id=tenant_id,
            creator_user_id=creator_user_id,
            creator_user_email=creator_user_email,
        )


async def superadmin_exists() -> bool:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-bootstrap") as conn:
        return await conn.fetchval(_SUPERADMIN_EXISTS)


async def seed_superadmin(*, email: str, password: str) -> dict[str, Any] | None:
    """No-op (None) if any superadmin exists, so restarts never reset changed
    credentials. Advisory-locked against concurrent seeds."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-bootstrap") as conn:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _BOOTSTRAP_LOCK_KEY)
        if await conn.fetchval(_SUPERADMIN_EXISTS):
            return None
        return await _insert_user(
            conn,
            email=email,
            password=password,
            role="superadmin",
            tenant_id=None,
            creator_user_id=None,
            creator_user_email=email,
        )


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "org"


async def register_admin(
    *,
    email: str,
    password: str,
    organization_name: str,
    first_name: str | None,
    last_name: str | None,
) -> dict[str, Any]:
    """Google signup (email already verified by Google) — see register_admin_on.
    Raises asyncpg.UniqueViolationError if the email is taken."""
    pool = await db.get_pool()
    async with platform_conn(pool, reason="pre-auth-register") as conn:
        return await register_admin_on(
            conn,
            email=email,
            password_hash=await asyncio.to_thread(auth.hash_password, password),
            organization_name=organization_name,
            first_name=first_name,
            last_name=last_name,
            password_set=False,
        )


async def register_admin_on(
    conn: Any,
    *,
    email: str,
    password_hash: str,
    organization_name: str,
    first_name: str | None,
    last_name: str | None,
    phone: str | None = None,
    signup_source: str | None = None,
    password_set: bool = True,
) -> dict[str, Any]:
    """A new tenant plus its first admin on the caller's transaction. The
    role is fixed here — no caller input can make this a superadmin."""
    slug = _slugify(organization_name)
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tenants WHERE slug = $1)", slug):
        slug = f"{slug}-{secrets.token_hex(3)}"
    tenant = dict(await conn.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", organization_name, slug,
    ))
    await audit.write_audit(
        conn, entity_type="tenant", entity_id=tenant["id"], action="created",
        user_id=None, user_email=email.lower(), new_value=tenant,
    )
    return await _insert_user(
        conn,
        email=email,
        password_hash=password_hash,
        role="admin",
        tenant_id=tenant["id"],
        creator_user_id=None,
        creator_user_email=email.lower(),
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        signup_source=signup_source,
        password_set=password_set,
    )


async def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """User row on success, else None for both unknown email and wrong password (no enumeration)."""
    user = await get_user_by_email(email)
    if user is None:
        return None
    if not await asyncio.to_thread(auth.verify_password, password, user["password_hash"]):
        return None
    return user


async def update_user(
    user_id: Any,
    *,
    actor_user_id: Any | None = None,
    actor_user_email: str | None = None,
    row_tenant_id: Any | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """`row_tenant_id` is the target row's tenant, already authorized by the router;
    None (platform-scoped row) is the only case that bypasses RLS."""
    if not fields:
        raise ValueError("update_user() called with no fields to update")

    new_password = fields.pop("password", None)

    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_user() got non-updatable field(s): {unknown}")

    if new_password is not None:
        fields["password_hash"] = await asyncio.to_thread(auth.hash_password, new_password)

    if not fields:
        raise ValueError("update_user() called with no fields to update")

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="users-admin-by-id", stamp_tenant=row_tenant_id)
        if row_tenant_id is None
        else tenant_conn(pool)
    )
    async with conn_cm as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id,
        )
        if old_row is None:
            raise LookupError(f"user {user_id} not found")
        old = dict(old_row)
        if old["is_service_account"]:
            raise ValueError(
                "cannot update a service account through this endpoint — "
                "manage it directly in Postgres",
            )

        columns = list(fields.keys())
        set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
        if new_password is not None:
            set_clause += ", token_version = token_version + 1"
        new_row = await conn.fetchrow(
            f"UPDATE users SET {set_clause}, updated_at = now() WHERE id = $1 RETURNING *",
            user_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

        # Only written columns, so a redacted password_hash doesn't appear changed on every update.
        await audit.write_audit(
            conn,
            entity_type="user",
            entity_id=user_id,
            action="updated",
            user_id=actor_user_id,
            user_email=actor_user_email,
            old_value={col: old[col] for col in columns},
            new_value={col: new[col] for col in columns},
        )
    return new


async def change_password(
    user_id: Any, *, current_password: str, new_password: str, platform_scoped: bool = False,
) -> dict[str, Any] | None:
    """Updated row with bumped token_version (revokes older sessions), or None if
    current_password doesn't match. Always the caller's own row."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="users-change-password") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id)
        if row is None:
            raise LookupError(f"user {user_id} not found")
        user = dict(row)
        # A Google-created account has no password to prove until it sets one.
        if user["password_set"] and not await asyncio.to_thread(
            auth.verify_password, current_password, user["password_hash"],
        ):
            return None

        new_hash = await asyncio.to_thread(auth.hash_password, new_password)
        updated = dict(await conn.fetchrow(
            "UPDATE users SET password_hash = $2, password_set = true, token_version = token_version + 1, "
            "updated_at = now() WHERE id = $1 RETURNING *",
            user_id, new_hash,
        ))
        # audit.py redacts password_hash before it reaches Postgres.
        await audit.write_audit(
            conn,
            entity_type="user",
            entity_id=user_id,
            action="updated",
            user_id=user_id,
            user_email=user["email"],
            old_value={"password_hash": user["password_hash"]},
            new_value={"password_hash": new_hash},
        )
    return updated


async def soft_delete_user(
    user_id: Any,
    *,
    actor_user_id: Any | None = None,
    actor_user_email: str | None = None,
    row_tenant_id: Any | None = None,
) -> None:
    """`row_tenant_id`: same contract as update_user()."""
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="users-admin-by-id", stamp_tenant=row_tenant_id)
        if row_tenant_id is None
        else tenant_conn(pool)
    )
    async with conn_cm as conn:
        old_row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id,
        )
        if old_row is None:
            raise LookupError(f"user {user_id} not found")
        old = dict(old_row)
        if old["is_service_account"]:
            raise ValueError(
                "cannot deactivate a service account — the platform depends on it "
                "staying active; manage it directly in Postgres if truly needed",
            )

        await conn.execute("UPDATE users SET deleted_at = now() WHERE id = $1", user_id)
        await audit.write_audit(
            conn,
            entity_type="user",
            entity_id=user_id,
            action="deleted",
            user_id=actor_user_id,
            user_email=actor_user_email,
            old_value=old,
        )
