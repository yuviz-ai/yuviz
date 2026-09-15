"""
Invite lifecycle — the pending/accepted/revoked state machine plus the one
permission gate (`may_invite`) every mutating path calls. Logic sits here,
not in routers/invites.py (Phase 3), same split as tenants.py/users.py.

Permission contract: `may_invite` is the single privilege-escalation check.
Create passes the *request's* target role/tenant; resend/revoke load the
row first and pass its *stored* role/tenant, so an enumerated invite id from
another tenant — or a superadmin's invite — is refused by the same rule
that blocks creating one (no IDOR, no inline if-chains).

Accept (`accept_invite`) is the one function with a real race: the
conditional `UPDATE ... WHERE status='pending' AND expires_at > now()
RETURNING *` is the serialisation point and runs *before* the users INSERT.
`accepted_user_id` is deliberately left unset by that UPDATE and backfilled
afterwards in the same transaction — setting it there would force the
INSERT to happen first, and the race loser would then serialize on
users_email_lower_idx and get a 500 instead of the promised 410.
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
    """409 at create — a live account already claims this email. tenant_name
    is None for the tenant-blind message a tenant_admin actor gets; set only
    when the actor is a super_admin."""

    def __init__(self, tenant_name: str | None = None):
        self.tenant_name = tenant_name


class PendingInviteConflict(Exception):
    """409 at create — a still-live (non-expired) pending invite already
    holds this (tenant, lower(email)) slot in user_invites_pending_email_idx.
    Distinct from EmailConflict (PR #19 finding 1): no account exists, the
    remedy is "revoke the stale invite first", and that's a different
    action from "this email cannot be invited". An *expired* pending invite
    never reaches this — create_invite revokes it in the same transaction
    before the INSERT, so only a genuinely live one collides. Carries no
    tenant name (unlike EmailConflict): the collision is always inside the
    actor's own tenant for a tenant_admin (may_invite requires
    actor_tenant_id == target_tenant_id), and a superadmin actor can
    already see every tenant via GET /users, so naming nothing here leaks
    nothing either way."""


class InviteExpired(Exception):
    pass


class InviteRevoked(Exception):
    pass


class InviteUsed(Exception):
    pass


class EmailTaken(Exception):
    """409 at accept — a squatting invite in another tenant for the same
    email was accepted first. Tenant-blind by construction: the accepting
    party is unauthenticated and must learn nothing about the other
    tenant."""


class ResendCooldown(Exception):
    """429 — a resend within RESEND_COOLDOWN_SECONDS of the last one. Raised
    from inside resend_invite's own locked transaction (not a router-level
    check-then-act) because the row is already loaded FOR UPDATE there —
    checking last_sent_at anywhere else would race two concurrent resends
    of the same invite (lesson 8)."""

    def __init__(self, remaining_seconds: int):
        self.remaining_seconds = remaining_seconds


class InviteContextGone(Exception):
    """410 at accept — the authorization this invite represents is no
    longer live: its target tenant was soft-deleted, or the inviter was
    soft-deleted/demoted such that may_invite would refuse this exact grant
    today. De-provisioning the inviter (or their tenant) does not
    de-provision what they already granted unless this is checked at
    accept time, since an invite can sit pending for up to 7 days.
    Tenant-blind and distinct from InviteExpired/InviteRevoked/InviteUsed
    (AC10) — the accepter learns only that the invite is no longer valid,
    never why."""


def may_invite(
    *, actor_role: str, actor_tenant_id: Any, target_role: str, target_tenant_id: Any,
) -> bool:
    """The privilege-escalation matrix (AC2/AC3/AC4). superadmin may invite
    anyone, anywhere. A tenant_admin (role="admin") may invite anyone except
    a superadmin, and only within its own tenant. Every other actor role —
    including supervisor/agent, which have no Config API surface at all —
    is denied unconditionally; belt and braces, since the console gate in
    deps.py already 403s them before any router body runs."""
    if actor_role == "superadmin":
        return True
    if actor_role == "admin":
        if target_role == "superadmin":
            return False
        if actor_tenant_id is None:
            return False
        return _same_tenant(actor_tenant_id, target_tenant_id)
    return False


def _same_tenant(a: Any, b: Any) -> bool:
    # actor_tenant_id/target_tenant_id may arrive as a str (JWT/request body)
    # on one side and a uuid.UUID (a stored row) on the other — compare by
    # string form so the two never spuriously mismatch on type alone.
    return (str(a) if a is not None else None) == (str(b) if b is not None else None)


def _new_token() -> tuple[str, str]:
    """secrets.token_urlsafe(32): 256 bits from os.urandom, ~43 URL-safe
    chars. Returns (raw, sha256 hex) — only the hash is ever stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


async def _conflict_tenant_name(tenant_id: Any, actor: CurrentUser) -> str | None:
    """None keeps the response tenant-blind for a tenant_admin actor. A
    super_admin actor already sees every tenant via GET /users, so the
    conflict names it (round-1 AC9, amended for the tenant-blind case)."""
    if actor.role != "superadmin":
        return None
    if tenant_id is None:
        return "platform"
    tenant = await tenants.get_tenant_by_id(tenant_id)
    return tenant["slug"] if tenant is not None else None


async def create_invite(
    *, email: str, role: str, tenant_id: Any | None, team: str | None, actor: CurrentUser,
) -> tuple[dict[str, Any], str]:
    """Returns (row, raw_token). Email delivery is the router's job
    (Phase 3), not this function's — a failed send must not undo the
    already-committed pending row."""
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
    # tenant_id is None only for a platform superadmin inviting another
    # platform admin — the row is tenant_id IS NULL by construction and
    # invisible to every policy, so that's the one legitimate bypass
    # (design's Tier 4: the router already asserted body.tenant_id against
    # the caller and set the target GUC to it for every other case).
    conn_cm = platform_conn(pool, reason="invites-create-null-tenant") if tenant_id is None else tenant_conn(pool)
    async with conn_cm as conn:
        # Self-heal (PR #19 finding 1, amended per round-2 review): an
        # expired invite is still status='pending' — expiry is derived,
        # never stored — so it would otherwise squat this (tenant,
        # lower(email)) slot in user_invites_pending_email_idx forever.
        # The unique index guarantees at most one pending row per slot,
        # so lock and inspect that one row rather than blind-UPDATE-ing
        # by (tenant, email) alone: reusing may_invite against its
        # *stored* role/tenant — exactly as revoke_invite does — means
        # this can only reclaim a slot the actor could already revoke
        # through POST /invites/{id}/revoke. A tenant_admin cannot use
        # a re-invite to silently clear an expired superadmin-role
        # invite it could never have revoked directly; that case falls
        # through untouched and the INSERT below correctly conflicts.
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
            # Distinct from an operator-initiated revoke (design line
            # 206 / AC13: every mutating path audits in the same
            # transaction) — the "reason" marker is what lets a reader
            # tell "the system reclaimed a dead slot" from "an admin
            # revoked this", even though both write action="updated".
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
            # A genuinely live pending invite for this slot — the
            # expired case was just revoked above, so this can only be
            # a concurrent double-send or a real, unexpired pending
            # invite. Distinct from EmailConflict: no account exists,
            # the remedy is "revoke the existing invite first".
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
    """Rotates token_hash in place and extends expires_at by another
    INVITE_TTL — a resend is a fresh grant, re-checked against the actor's
    current role/tenant by may_invite below, so the invitee's new link must
    not inherit the old grant's stale expiry (PR #19 finding 2). 404s before
    it 403s (row loaded first), then may_invite runs against the *stored*
    role/tenant, not anything the caller supplies — an IDOR against another
    tenant's invite, or a superadmin's, is refused the same way creating one
    would be."""
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
    """Tier 3 by-id resolver for the router's pre-mutation authorization
    (resend/revoke) — same convention as every other by-id getter:
    `platform_scoped` (deps.is_platform_scoped(current_user) only, lesson
    24) selects platform_conn for a platform actor's read."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="invites-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow("SELECT * FROM user_invites WHERE id = $1", invite_id)
    return dict(row) if row is not None else None


async def _check_context_live(conn: Any, invite: dict[str, Any]) -> None:
    """Raises InviteContextGone if the granting context is no longer live:
    the invite's target tenant was soft-deleted, or the inviter is gone /
    no longer holds a role that could have issued this exact grant
    (re-checked with may_invite against their CURRENT role/tenant, not what
    was true when they sent it). Shared by accept_invite (PR #19 finding
    3) — under the invite row's FOR UPDATE lock, atomic with the accept
    itself — and get_invite_for_accept, which is read-only and needs no
    lock; `conn` may be either a transaction connection or the bare pool,
    both expose fetchrow. De-provisioning the inviter or their tenant does
    not de-provision what they already granted unless this is checked."""
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
    """Public — no actor, no JWT. Raises InviteExpired/InviteRevoked/
    InviteUsed/InviteContextGone/EmailTaken/LookupError, each distinct
    (AC6/AC10)."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    pool = await db.get_pool()
    # Pre-auth: no actor, no JWT, and the invite/new-user row may itself be
    # NULL-tenant (a platform admin invite) — platform_conn bypass, same as
    # every other pre-auth path in this module.
    async with platform_conn(pool, reason="pre-auth-invite-accept") as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_invites WHERE token_hash = $1 FOR UPDATE", token_hash,
        )
        if row is None:
            raise LookupError("invite not found")
        invite = dict(row)

        # Classification only — the conditional UPDATE below is the
        # actual serialisation point for a concurrent accept.
        if invite["status"] == "revoked":
            raise InviteRevoked()
        if invite["status"] == "accepted":
            raise InviteUsed()
        if invite["expires_at"] < datetime.now(timezone.utc):
            raise InviteExpired()

        # Re-validate the granting context is still alive — done here,
        # holding the invite row's lock (acquired by the SELECT ... FOR
        # UPDATE above) and before the conditional UPDATE below, so it
        # is atomic with the accept itself and cannot race a concurrent
        # revoke.
        await _check_context_live(conn, invite)

        updated = await conn.fetchrow(
            "UPDATE user_invites SET status = 'accepted', accepted_at = now(), updated_at = now() "
            "WHERE id = $1 AND status = 'pending' AND expires_at > now() "
            "RETURNING *",
            invite["id"],
        )
        if updated is None:
            # Lost the race between the SELECT above and here — the
            # winner's transaction already flipped this row.
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
            # Squatting invite in another tenant for this email accepted
            # first (finding 3). Rolling back leaves this invite
            # pending — resendable/revocable — instead of burning it.
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
    """Strips token_hash — never returned to a client, same treatment
    users.to_public_dict gives password_hash."""
    return {k: v for k, v in invite.items() if k != "token_hash"}


async def list_invites(*, tenant_id: Any | None, is_superadmin: bool) -> list[dict[str, Any]]:
    """`tenant_id=None` with `is_superadmin=True` means "no filter given" —
    every tenant. Any other combination filters to that exact tenant_id
    (including None, a superadmin-scoped invite) — the router is what
    forces tenant_id to the JWT's own for a non-superadmin actor, this
    function never trusts a caller-supplied value on its own (CURSOR.md:
    never trust client tenancy)."""
    pool = await db.get_pool()
    # tenant_id=None is, by construction, a listing no RLS policy can ever
    # return (cross-tenant, or a NULL-tenant-scoped filter) — platform_conn
    # regardless of is_superadmin, same rule as users.list_users.
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
    """Read-only classification for GET /invites/accept — the same status
    checks and granting-context re-validation accept_invite runs, without
    locking or mutating anything (PR #19 finding 3: this used to skip
    _check_context_live entirely, so GET kept returning a live-looking
    invite for the full 7-day TTL after POST would already 410 it). Returns
    only the invitee's own email/tenant/role/status, nothing about any
    other account (AC per design's HTTP section)."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    pool = await db.get_pool()
    # Pre-auth, same as accept_invite: no actor, and the invite may itself
    # be NULL-tenant.
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
