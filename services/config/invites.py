"""
Invite lifecycle (pending/accepted/revoked). `may_invite` is the single
privilege check; resend/revoke apply it to the *stored* role/tenant (no IDOR).

accept_invite's conditional UPDATE runs before the users INSERT so the race
loser gets 410, not a unique-index 500; accepted_user_id is backfilled after.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

import asyncpg

from libs.tenancy import platform_conn, tenant_conn

from . import audit, db, tenants
from . import users as users_service
from .auth import CurrentUser

INVITE_TTL = "7 days"
RESEND_COOLDOWN_SECONDS = 60


class PermissionDenied(Exception):
    """may_invite() returned False for this actor/target combination."""


class InviteNotPending(Exception):
    """resend/revoke on an invite that is already accepted or revoked."""


class EmailConflict(Exception):
    """409 at create: an account has this email. tenant_name set only for superadmin actors."""

    def __init__(self, tenant_name: str | None = None):
        self.tenant_name = tenant_name


class PendingInviteConflict(Exception):
    """409 at create: a live pending invite holds this (tenant, email) slot; revoke it first."""


class InviteExpired(Exception):
    pass


class InviteRevoked(Exception):
    pass


class InviteUsed(Exception):
    pass


class EmailTaken(Exception):
    """409 at accept: another invite for this email was accepted first. Tenant-blind."""


class ResendCooldown(Exception):
    """429: resend within the cooldown. Checked under the row lock to avoid racing resends."""

    def __init__(self, remaining_seconds: int):
        self.remaining_seconds = remaining_seconds


class InviteContextGone(Exception):
    """410 at accept: target tenant deleted or inviter could no longer grant this. Tenant-blind."""


def may_invite(
    *, actor_role: str, actor_tenant_id: Any, target_role: str, target_tenant_id: Any,
) -> bool:
    """Nobody may invite a superadmin (seeded only). superadmin invites any
    other role anywhere; admin only within its own tenant; everyone else, never."""
    if target_role == "superadmin":
        return False
    if actor_role == "superadmin":
        return True
    if actor_role == "admin":
        if actor_tenant_id is None:
            return False
        return _same_tenant(actor_tenant_id, target_tenant_id)
    return False


def _same_tenant(a: Any, b: Any) -> bool:
    # str (JWT/body) vs uuid.UUID (row): compare string forms.
    return (str(a) if a is not None else None) == (str(b) if b is not None else None)


def _new_token() -> tuple[str, str]:
    """Returns (raw 256-bit token, sha256 hex); only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


async def _conflict_tenant_name(tenant_id: Any, actor: CurrentUser) -> str | None:
    """Tenant name for superadmin actors only; None keeps it tenant-blind."""
    if actor.role != "superadmin":
        return None
    if tenant_id is None:
        return "platform"
    tenant = await tenants.get_tenant_by_id(tenant_id)
    return tenant["slug"] if tenant is not None else None


async def create_invite(
    *, email: str, role: str, tenant_id: Any | None, team: str | None, actor: CurrentUser,
) -> tuple[dict[str, Any], str]:
    """Returns (row, raw_token); the router sends email so a failed send can't undo the row."""
    if not may_invite(
        actor_role=actor.role, actor_tenant_id=actor.tenant_id,
        target_role=role, target_tenant_id=tenant_id,
    ):
        raise PermissionDenied()

    email = email.lower()

    existing_user = await users_service.get_user_by_email(email)
    if existing_user is not None:
        raise EmailConflict(await _conflict_tenant_name(existing_user["tenant_id"], actor))

    raw_token, token_hash = _new_token()
    pool = await db.get_pool()
    # NULL-tenant rows are invisible to RLS; the router already asserted tenant access.
    conn_cm = platform_conn(pool, reason="invites-create-null-tenant") if tenant_id is None else tenant_conn(pool)
    async with conn_cm as conn:
        # Expired invites stay 'pending' and squat the slot; reclaim it only if
        # the actor could revoke it (may_invite on the stored role/tenant).
        slot = await conn.fetchrow(
            "SELECT * FROM user_invites WHERE status = 'pending' "
            "AND COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid) "
            "= COALESCE($1, '00000000-0000-0000-0000-000000000000'::uuid) "
            "AND lower(email) = $2 FOR UPDATE",
            tenant_id, email,
        )
        if (
            slot is not None
            and slot["expires_at"] <= datetime.now(timezone.utc)
            and may_invite(
                actor_role=actor.role, actor_tenant_id=actor.tenant_id,
                target_role=slot["role"], target_tenant_id=slot["tenant_id"],
            )
        ):
            reclaimed = await conn.fetchrow(
                "UPDATE user_invites SET status = 'revoked', updated_at = now() "
                "WHERE id = $1 RETURNING *",
                slot["id"],
            )
            # "reason" distinguishes a system reclaim from an admin revoke.
            await audit.write_audit(
                conn,
                entity_type="invite",
                entity_id=reclaimed["id"],
                action="updated",
                user_id=actor.id,
                user_email=actor.email,
                old_value={"status": "pending"},
                new_value={"status": "revoked", "reason": "expired_reclaimed_on_reinvite"},
            )
        try:
            row = await conn.fetchrow(
                "INSERT INTO user_invites "
                "(tenant_id, email, role, team, token_hash, expires_at, invited_by, last_sent_at) "
                f"VALUES ($1, $2, $3, $4, $5, now() + interval '{INVITE_TTL}', $6, now()) "
                "RETURNING *",
                tenant_id, email, role, team, token_hash, actor.id,
            )
        except asyncpg.UniqueViolationError:
            raise PendingInviteConflict()
        result = dict(row)
        await audit.write_audit(
            conn,
            entity_type="invite",
            entity_id=result["id"],
            action="created",
            user_id=actor.id,
            user_email=actor.email,
            new_value=result,
        )
    return result, raw_token


async def resend_invite(invite_id: Any, *, actor: CurrentUser) -> tuple[dict[str, Any], str]:
    """Rotate the token and extend expiry (a fresh grant); may_invite checks the stored role/tenant."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow("SELECT * FROM user_invites WHERE id = $1 FOR UPDATE", invite_id)
        if row is None:
            raise LookupError(f"invite {invite_id} not found")
        invite = dict(row)
        if not may_invite(
            actor_role=actor.role, actor_tenant_id=actor.tenant_id,
            target_role=invite["role"], target_tenant_id=invite["tenant_id"],
        ):
            raise PermissionDenied()
        if invite["status"] != "pending":
            raise InviteNotPending()
        if invite["last_sent_at"] is not None:
            elapsed = (datetime.now(timezone.utc) - invite["last_sent_at"]).total_seconds()
            if elapsed < RESEND_COOLDOWN_SECONDS:
                raise ResendCooldown(int(RESEND_COOLDOWN_SECONDS - elapsed) + 1)

        raw_token, token_hash = _new_token()
        updated = await conn.fetchrow(
            "UPDATE user_invites SET token_hash = $2, last_sent_at = now(), updated_at = now(), "
            f"expires_at = now() + interval '{INVITE_TTL}' "
            "WHERE id = $1 RETURNING *",
            invite_id, token_hash,
        )
        result = dict(updated)
        await audit.write_audit(
            conn,
            entity_type="invite",
            entity_id=invite_id,
            action="updated",
            user_id=actor.id,
            user_email=actor.email,
            old_value={"token_hash": invite["token_hash"]},
            new_value={"token_hash": result["token_hash"]},
        )
    return result, raw_token


async def revoke_invite(invite_id: Any, *, actor: CurrentUser) -> dict[str, Any]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow("SELECT * FROM user_invites WHERE id = $1 FOR UPDATE", invite_id)
        if row is None:
            raise LookupError(f"invite {invite_id} not found")
        invite = dict(row)
        if not may_invite(
            actor_role=actor.role, actor_tenant_id=actor.tenant_id,
            target_role=invite["role"], target_tenant_id=invite["tenant_id"],
        ):
            raise PermissionDenied()
        if invite["status"] != "pending":
            raise InviteNotPending()

        updated = await conn.fetchrow(
            "UPDATE user_invites SET status = 'revoked', updated_at = now() "
            "WHERE id = $1 RETURNING *",
            invite_id,
        )
        result = dict(updated)
        await audit.write_audit(
            conn,
            entity_type="invite",
            entity_id=invite_id,
            action="updated",
            user_id=actor.id,
            user_email=actor.email,
            old_value={"status": invite["status"]},
            new_value={"status": result["status"]},
        )
    return result


async def get_invite_by_id(invite_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """By-id read for router authorization; platform_scoped selects platform_conn."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="invites-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow("SELECT * FROM user_invites WHERE id = $1", invite_id)
    return dict(row) if row is not None else None


async def _check_context_live(conn: Any, invite: dict[str, Any]) -> None:
    """Raise InviteContextGone if the tenant is deleted or the inviter's CURRENT
    role/tenant could no longer issue this grant."""
    if invite["tenant_id"] is not None:
        tenant_row = await conn.fetchrow(
            "SELECT 1 FROM tenants WHERE id = $1 AND deleted_at IS NULL",
            invite["tenant_id"],
        )
        if tenant_row is None:
            raise InviteContextGone()

    inviter = None
    if invite["invited_by"] is not None:
        inviter = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL",
            invite["invited_by"],
        )
    if inviter is None:
        raise InviteContextGone()
    if not may_invite(
        actor_role=inviter["role"], actor_tenant_id=inviter["tenant_id"],
        target_role=invite["role"], target_tenant_id=invite["tenant_id"],
    ):
        raise InviteContextGone()


async def accept_invite(*, raw_token: str, password: str) -> dict[str, Any]:
    """Public, unauthenticated accept; raises a distinct exception per failure."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    pool = await db.get_pool()
    # Pre-auth and possibly NULL-tenant, so platform_conn.
    async with platform_conn(pool, reason="pre-auth-invite-accept") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_invites WHERE token_hash = $1 FOR UPDATE", token_hash,
        )
        if row is None:
            raise LookupError("invite not found")
        invite = dict(row)

        # Classification only; the conditional UPDATE below serializes accepts.
        if invite["status"] == "revoked":
            raise InviteRevoked()
        if invite["status"] == "accepted":
            raise InviteUsed()
        if invite["expires_at"] < datetime.now(timezone.utc):
            raise InviteExpired()

        # Under the row lock, so it can't race a concurrent revoke.
        await _check_context_live(conn, invite)

        updated = await conn.fetchrow(
            "UPDATE user_invites SET status = 'accepted', accepted_at = now(), updated_at = now() "
            "WHERE id = $1 AND status = 'pending' AND expires_at > now() "
            "RETURNING *",
            invite["id"],
        )
        if updated is None:
            raise InviteUsed()

        try:
            user = await users_service._insert_user(
                conn,
                email=invite["email"],
                password=password,
                role=invite["role"],
                tenant_id=invite["tenant_id"],
                team=invite["team"],
                creator_user_id=None,
                creator_user_email=invite["email"],
            )
        except asyncpg.UniqueViolationError:
            # Rollback leaves this invite pending rather than burning it.
            raise EmailTaken()

        await conn.execute(
            "UPDATE user_invites SET accepted_user_id = $2 WHERE id = $1",
            invite["id"], user["id"],
        )
        await audit.write_audit(
            conn,
            entity_type="invite",
            entity_id=invite["id"],
            action="updated",
            user_id=user["id"],
            user_email=user["email"],
            new_value={"status": "accepted"},
        )
    return user


def to_public_dict(invite: dict[str, Any]) -> dict[str, Any]:
    """Strip token_hash, which is never returned to a client."""
    return {k: v for k, v in invite.items() if k != "token_hash"}


async def list_invites(*, tenant_id: Any | None, is_superadmin: bool) -> list[dict[str, Any]]:
    """Superadmin with tenant_id=None lists every tenant; otherwise filter to exactly tenant_id.

    The router forces tenant_id to the caller's own for non-superadmins."""
    pool = await db.get_pool()
    # No RLS policy can return a tenant_id=None listing.
    conn_cm = platform_conn(pool, reason="invites-null-tenant-listing") if tenant_id is None else tenant_conn(pool)
    async with conn_cm as conn:
        if is_superadmin and tenant_id is None:
            rows = await conn.fetch("SELECT * FROM user_invites ORDER BY created_at DESC")
        else:
            rows = await conn.fetch(
                "SELECT * FROM user_invites WHERE tenant_id IS NOT DISTINCT FROM $1 ORDER BY created_at DESC",
                tenant_id,
            )
    return [dict(row) for row in rows]


async def get_invite_for_accept(*, raw_token: str) -> dict[str, Any]:
    """Read-only version of accept_invite's checks for GET /invites/accept.

    Returns only the invitee's own email/tenant/role/status."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    pool = await db.get_pool()
    # Pre-auth and possibly NULL-tenant, so platform_conn.
    async with platform_conn(pool, reason="pre-auth-invite-accept") as conn:
        row = await conn.fetchrow("SELECT * FROM user_invites WHERE token_hash = $1", token_hash)
        if row is None:
            raise LookupError("invite not found")
        invite = dict(row)
        if invite["status"] == "revoked":
            raise InviteRevoked()
        if invite["status"] == "accepted":
            raise InviteUsed()
        if invite["expires_at"] < datetime.now(timezone.utc):
            raise InviteExpired()
        await _check_context_live(conn, invite)

    tenant_name = "platform"
    if invite["tenant_id"] is not None:
        tenant = await tenants.get_tenant_by_id(invite["tenant_id"])
        tenant_name = tenant["name"] if tenant is not None else "platform"
    return {
        "email": invite["email"],
        "tenant_name": tenant_name,
        "role": invite["role"],
        "status": invite["status"],
    }
