"""
T7-T10 — may_invite, token generation, and the create/resend/revoke/accept
lifecycle. Runs against real Postgres (same convention as conftest.py's
other fixtures) since accept_invite's whole point is a transaction that
behaves correctly under real Postgres semantics, not a mock.

No router exists yet (routers/invites.py is a Phase 3 task) — everything
here calls invites.py's functions directly. The exact HTTP status/body
mapping this file's "done when" criteria describe in HTTP terms (409, 403,
...) is therefore verified at the function-exception level (EmailConflict,
PermissionDenied, ...) and is NOT exercised end-to-end through the app;
that end-to-end check belongs to Phase 3's test_invites.py additions once
routers/invites.py exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path

import asyncpg
import pytest

from services.config import invites
from services.config import users as users_service
from services.config.auth import CurrentUser

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_SQL = (REPO_ROOT / "database" / "schema.sql").read_text()


def _actor(user: dict) -> CurrentUser:
    return CurrentUser(
        id=str(user["id"]), email=user["email"], role=user["role"],
        tenant_id=str(user["tenant_id"]) if user["tenant_id"] is not None else None,
    )


async def _cleanup_invite(pool, invite_id):
    await pool.execute("DELETE FROM user_invites WHERE id = $1", invite_id)


async def _cleanup_user_by_email(pool, email):
    # Soft-delete, not hard: accept_invite's own audit_log row (and
    # _insert_user's) references this id via audit_log_user_id_fkey — same
    # constraint conftest.py's test_superadmin/test_admin fixtures document.
    await pool.execute("UPDATE users SET deleted_at = now() WHERE lower(email) = lower($1)", email)


class TestMayInvite:
    """The privilege-escalation matrix (AC2/AC3/AC4), pure — no DB."""

    TENANT_A = "11111111-1111-1111-1111-111111111111"
    TENANT_B = "22222222-2222-2222-2222-222222222222"

    def test_tenant_admin_cannot_invite_superadmin(self):
        assert invites.may_invite(
            actor_role="admin", actor_tenant_id=self.TENANT_A,
            target_role="superadmin", target_tenant_id=self.TENANT_A,
        ) is False

    @pytest.mark.parametrize("target_tenant_id", [TENANT_B, None])
    def test_tenant_admin_cannot_invite_outside_own_tenant(self, target_tenant_id):
        assert invites.may_invite(
            actor_role="admin", actor_tenant_id=self.TENANT_A,
            target_role="admin", target_tenant_id=target_tenant_id,
        ) is False

    @pytest.mark.parametrize("target_role", ["admin", "supervisor", "agent", "viewer"])
    def test_tenant_admin_may_invite_own_tenant(self, target_role):
        assert invites.may_invite(
            actor_role="admin", actor_tenant_id=self.TENANT_A,
            target_role=target_role, target_tenant_id=self.TENANT_A,
        ) is True

    def test_superadmin_may_invite_tenant_admin_anywhere(self):
        assert invites.may_invite(
            actor_role="superadmin", actor_tenant_id=None,
            target_role="admin", target_tenant_id=self.TENANT_B,
        ) is True

    def test_superadmin_may_invite_superadmin(self):
        assert invites.may_invite(
            actor_role="superadmin", actor_tenant_id=None,
            target_role="superadmin", target_tenant_id=None,
        ) is True

    @pytest.mark.parametrize("target_role", ["admin", "viewer"])
    @pytest.mark.parametrize("target_tenant_id", [None, TENANT_A])
    def test_tenant_less_admin_cannot_invite(self, target_role, target_tenant_id):
        # PR #19 finding 4: _same_tenant(None, None) was True, so a
        # platform-scoped ("tenant-less") admin could mint further
        # platform-scope admin/viewer accounts indefinitely with no
        # superadmin in the loop — the only account-creation path now that
        # POST /users is gone. The admin branch now requires a real,
        # non-NULL actor tenant.
        assert invites.may_invite(
            actor_role="admin", actor_tenant_id=None,
            target_role=target_role, target_tenant_id=target_tenant_id,
        ) is False

    @pytest.mark.parametrize("actor_role", ["supervisor", "agent", "viewer"])
    @pytest.mark.parametrize("target_role", ["superadmin", "admin", "supervisor", "agent", "viewer"])
    def test_non_console_and_viewer_actors_are_always_denied(self, actor_role, target_role):
        assert invites.may_invite(
            actor_role=actor_role, actor_tenant_id=self.TENANT_A,
            target_role=target_role, target_tenant_id=self.TENANT_A,
        ) is False

    def test_every_role_in_users_role_check_is_classified(self):
        # Ground truth is parsed live out of database/schema.sql's actual
        # `ADD CONSTRAINT users_role_check` statement, not hand-copied — a
        # role added there and never mentioned below now genuinely trips
        # this test (the previous version's `role == "viewer"` shortcut
        # made the CONSOLE_ROLES half of the check pass vacuously for
        # viewer no matter what CONSOLE_ROLES contained; this version
        # doesn't have a viewer-shaped escape hatch).
        match = re.search(
            r"ADD CONSTRAINT users_role_check\s*\n\s*CHECK \(role IN \(([^)]*)\)\)",
            SCHEMA_SQL,
        )
        assert match is not None, "users_role_check constraint not found in schema.sql"
        roles_in_schema = {r.strip().strip("'") for r in match.group(1).split(",")}

        # Every role's console-vs-non-console *and* can-invite-something
        # classification is an explicit, named decision here — not derived
        # from whatever deps.CONSOLE_ROLES happens to hold, so a new role
        # missing from this dict fails via the set-equality assertion just
        # below, and a role misclassified in *either* dimension fails via
        # the per-role assertions after it.
        expected = {
            "superadmin": {"console": True, "can_invite_something": True},
            "admin": {"console": True, "can_invite_something": True},
            "viewer": {"console": True, "can_invite_something": False},
            "supervisor": {"console": False, "can_invite_something": False},
            "agent": {"console": False, "can_invite_something": False},
        }
        assert roles_in_schema == set(expected), (
            f"schema.sql roles {roles_in_schema} != classified roles {set(expected)} "
            "— a role was added or removed without updating this test's classification"
        )

        from services.config.deps import CONSOLE_ROLES
        for role, want in expected.items():
            assert (role in CONSOLE_ROLES) is want["console"], role
            can_invite_something = invites.may_invite(
                actor_role=role, actor_tenant_id=self.TENANT_A,
                target_role="viewer", target_tenant_id=self.TENANT_A,
            )
            assert can_invite_something is want["can_invite_something"], role


class TestNewToken:
    def test_raw_token_is_at_least_43_chars(self):
        raw, _ = invites._new_token()
        assert len(raw) >= 43

    def test_hash_is_sha256_of_raw(self):
        raw, token_hash = invites._new_token()
        assert token_hash == hashlib.sha256(raw.encode()).hexdigest()

    def test_no_collisions_in_1000_calls(self):
        raws = set()
        hashes = set()
        for _ in range(1000):
            raw, token_hash = invites._new_token()
            raws.add(raw)
            hashes.add(token_hash)
        assert len(raws) == 1000
        assert len(hashes) == 1000


class TestCreateInvite:
    async def test_denies_privilege_escalation(self, scoped, pool, test_admin, test_tenant):
        with pytest.raises(invites.PermissionDenied):
            await invites.create_invite(
                email="nobody@example.com", role="superadmin", tenant_id=test_tenant["id"],
                team=None, actor=_actor(test_admin["user"]),
            )

    async def test_success_returns_row_and_raw_token_and_audits(self, scoped, pool, test_admin, test_tenant):
        email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
        try:
            row, raw_token = await invites.create_invite(
                email=email, role="viewer", tenant_id=test_tenant["id"], team="support",
                actor=_actor(test_admin["user"]),
            )
            assert row["email"] == email
            assert row["status"] == "pending"
            assert len(raw_token) >= 43

            audit_row = await pool.fetchrow(
                "SELECT * FROM audit_log WHERE entity_type = 'invite' AND entity_id = $1 "
                "ORDER BY changed_at DESC LIMIT 1",
                row["id"],
            )
            assert audit_row is not None
            # The stored token_hash really is sha256(raw_token) — ties the
            # raw value to what got persisted, so a bug that stored the raw
            # token itself (instead of its hash) would show up here as a
            # mismatch, not as an absence.
            assert row["token_hash"] == hashlib.sha256(raw_token.encode()).hexdigest()
            # Checked across the *whole* audit row (every column, not just
            # new_value) — create_invite's only chance to leak the raw
            # token into audit_log is by accident, so this is a real
            # negative-space check, not a restatement of "new_value has no
            # raw_token key" (which the row's own shape already guarantees
            # and so could never fail).
            assert raw_token not in str(dict(audit_row))
            new_value = json.loads(audit_row["new_value"])
            assert new_value["token_hash"] == "[redacted]"
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_cross_tenant_conflict_is_tenant_blind_and_matches_own_tenant_conflict(
        self, scoped, pool, test_admin, test_tenant, test_superadmin,
    ):
        # The actual CRITICAL finding this code exists to fix: a live
        # account in a DIFFERENT tenant than the actor's must not leak that
        # tenant's name/slug/id to a tenant_admin. The email/tenant_id this
        # test invites into is test_tenant (the actor's own tenant) — the
        # *existing* account it collides with lives in `other`, a genuinely
        # different tenant, so the cross-tenant branch at invites.py's
        # _conflict_tenant_name is actually constructed (a same-tenant
        # collision, which the old version of this test used, never reaches
        # that branch's tenant-comparison at all).
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Test Other Conflict", f"test-conflict-{uuid.uuid4().hex[:8]}",
        )
        other_user_email = f"other-tenant-user-{uuid.uuid4().hex[:8]}@example.com"
        other_user = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, tenant_id) "
            "VALUES ($1, 'x', 'viewer', $2) RETURNING *",
            other_user_email, other["id"],
        )
        try:
            with pytest.raises(invites.EmailConflict) as cross_tenant_exc:
                await invites.create_invite(
                    email=other_user_email, role="viewer", tenant_id=test_tenant["id"],
                    team=None, actor=_actor(test_admin["user"]),
                )
            # No trace of `other` (name, slug or id) anywhere in the
            # exception's own data — tenant_name is the only field the
            # eventual HTTP layer (Phase 3) formats a detail string from, so
            # asserting it here is the byte-identical body's actual source.
            assert cross_tenant_exc.value.tenant_name is None
            assert other["slug"] not in repr(cross_tenant_exc.value)
            assert str(other["id"]) not in repr(cross_tenant_exc.value)

            # Own-tenant conflict (existing account already lives in the
            # actor's own tenant) must produce the identical tenant_name —
            # None in both cases — so a tenant_admin can't distinguish "an
            # account exists elsewhere" from "an account exists here" (the
            # probe this design closes).
            own_tenant_email = test_admin["user"]["email"]
            with pytest.raises(invites.EmailConflict) as own_tenant_exc:
                await invites.create_invite(
                    email=own_tenant_email, role="viewer", tenant_id=test_tenant["id"],
                    team=None, actor=_actor(test_admin["user"]),
                )
            assert own_tenant_exc.value.tenant_name is cross_tenant_exc.value.tenant_name is None

            # The identical cross-tenant request as super_admin DOES name
            # the tenant — the positive case, since the platform operator
            # already sees every tenant via GET /users.
            with pytest.raises(invites.EmailConflict) as superadmin_exc:
                await invites.create_invite(
                    email=other_user_email, role="admin", tenant_id=test_tenant["id"],
                    team=None, actor=_actor(test_superadmin["user"]),
                )
            assert superadmin_exc.value.tenant_name == other["slug"]
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", other_user["id"])
            await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_duplicate_pending_invite_same_tenant_is_conflict(self, scoped, pool, test_admin, test_tenant):
        # PR #19 finding 1: a still-*live* pending invite raises the
        # distinct PendingInviteConflict, not EmailConflict — no account
        # exists here, the remedy is "revoke the existing invite first".
        email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
        row, _ = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            with pytest.raises(invites.PendingInviteConflict):
                await invites.create_invite(
                    email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
                    actor=_actor(test_admin["user"]),
                )
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_expired_pending_invite_is_self_healed_and_does_not_block_reinvite(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # PR #19 finding 1 (BLOCKING): expiry is derived, not stored, so an
        # expired invite is still status='pending' and would otherwise
        # squat this (tenant, lower(email)) slot in
        # user_invites_pending_email_idx forever. create_invite must revoke
        # the stale row itself and let the re-invite through. Mutation-
        # verified: removing the pre-INSERT revoke UPDATE in create_invite
        # makes this fail with PendingInviteConflict instead of a second
        # successful insert.
        email = f"stale-{uuid.uuid4().hex[:8]}@example.com"
        old_row, _ = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        await pool.execute(
            "UPDATE user_invites SET expires_at = now() - interval '1 day' WHERE id = $1",
            old_row["id"],
        )
        try:
            new_row, _ = await invites.create_invite(
                email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
                actor=_actor(test_admin["user"]),
            )
            assert new_row["id"] != old_row["id"]
            stale = await pool.fetchrow("SELECT status FROM user_invites WHERE id = $1", old_row["id"])
            assert stale["status"] == "revoked"

            # PR #19 round-2 finding: the reclaim must audit, and must be
            # distinguishable from an operator-initiated revoke.
            audit_row = await pool.fetchrow(
                "SELECT * FROM audit_log WHERE entity_type = 'invite' AND entity_id = $1 "
                "AND action = 'updated' ORDER BY changed_at DESC LIMIT 1",
                old_row["id"],
            )
            assert audit_row is not None
            assert audit_row["user_email"] == test_admin["user"]["email"]
            assert "expired_reclaimed_on_reinvite" in audit_row["new_value"]

            await _cleanup_invite(pool, new_row["id"])
        finally:
            await _cleanup_invite(pool, old_row["id"])

    async def test_self_heal_does_not_reclaim_a_role_the_actor_could_not_revoke(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # PR #19 round-2 finding [low, security]: revoke_invite refuses
        # target_role='superadmin' for an admin actor (may_invite). The
        # self-heal must reuse that same check against the *stored* row's
        # role, not just match on (tenant, email) — otherwise a
        # tenant_admin could clear an expired superadmin-role invite it
        # could never have revoked through POST /invites/{id}/revoke, with
        # no way to tell afterward that it happened. Mutation-verified:
        # dropping the `may_invite(...)` guard from the self-heal condition
        # makes this fail — the stale row is silently revoked instead of
        # staying pending, and create_invite raises no conflict.
        email = f"escalate-{uuid.uuid4().hex[:8]}@example.com"
        stale = await pool.fetchrow(
            "INSERT INTO user_invites "
            "(tenant_id, email, role, token_hash, expires_at, invited_by, last_sent_at) "
            "VALUES ($1, $2, 'superadmin', $3, now() - interval '1 day', $4, now()) RETURNING *",
            test_tenant["id"], email, hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
            test_admin["user"]["id"],
        )
        try:
            with pytest.raises(invites.PendingInviteConflict):
                await invites.create_invite(
                    email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
                    actor=_actor(test_admin["user"]),
                )
            # Untouched, not silently reclaimed.
            refreshed = await pool.fetchrow("SELECT status FROM user_invites WHERE id = $1", stale["id"])
            assert refreshed["status"] == "pending"
            audit_count = await pool.fetchval(
                "SELECT count(*) FROM audit_log WHERE entity_type = 'invite' AND entity_id = $1 "
                "AND action = 'updated'",
                stale["id"],
            )
            assert audit_count == 0
        finally:
            await _cleanup_invite(pool, stale["id"])

    async def test_same_email_different_tenants_both_insert(self, scoped, pool, test_admin, test_tenant):
        # A second, independent tenant so the pending-email index's per-
        # tenant scope (T5) is exercised from the invites.py side too.
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Test Other", f"test-other-{uuid.uuid4().hex[:8]}",
        )
        email = f"cross-{uuid.uuid4().hex[:8]}@example.com"
        row_a, _ = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        other_admin = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, tenant_id) VALUES ($1, 'x', 'admin', $2) RETURNING *",
            f"other-admin-{uuid.uuid4().hex[:8]}@example.com", other["id"],
        )
        try:
            row_b, _ = await invites.create_invite(
                email=email, role="viewer", tenant_id=other["id"], team=None,
                actor=_actor(dict(other_admin)),
            )
            assert row_a["id"] != row_b["id"]
            await _cleanup_invite(pool, row_b["id"])
        finally:
            await _cleanup_invite(pool, row_a["id"])
            # Soft-delete: other_admin is the actor on this test's own
            # create_invite audit_log row (same FK as _cleanup_user_by_email).
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", other_admin["id"])
            await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


    async def test_conflict_check_is_case_insensitive_against_existing_account(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # Design test plan, "Normalization (finding 8, round 1)": an account
        # created as bob@x.com must block an invite to Bob@X.com. Genuinely
        # gapped before this — the suite only ever exercised exact-case
        # collisions. Would fail (create_invite succeeds instead of raising)
        # if get_user_by_email's lower(email) comparison regressed to a
        # case-sensitive one.
        email_lower = f"case-{uuid.uuid4().hex[:8]}@example.com"
        existing = await pool.fetchrow(
            "INSERT INTO users (email, password_hash, role, tenant_id) VALUES ($1, 'x', 'viewer', $2) RETURNING *",
            email_lower, test_tenant["id"],
        )
        try:
            with pytest.raises(invites.EmailConflict):
                await invites.create_invite(
                    email=email_lower.upper(), role="viewer", tenant_id=test_tenant["id"],
                    team=None, actor=_actor(test_admin["user"]),
                )
        finally:
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", existing["id"])


class TestResendAndRevoke:
    async def test_resend_rotates_token_old_404s_new_accepts(self, scoped, pool, test_admin, test_tenant):
        email = f"resend-{uuid.uuid4().hex[:8]}@example.com"
        row, old_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            # create_invite's own INSERT sets last_sent_at = now(), so an
            # immediate resend would trip the 60s cooldown below (a
            # separate, already-covered path) rather than exercise the
            # token rotation this test is actually about. Backdate it past
            # the window first.
            await pool.execute(
                "UPDATE user_invites SET last_sent_at = now() - interval '61 seconds' WHERE id = $1",
                row["id"],
            )
            updated, new_token = await invites.resend_invite(row["id"], actor=_actor(test_admin["user"]))
            assert updated["token_hash"] != row["token_hash"]

            # T12's redaction matters most here, not at create: resend is
            # the one path that writes token_hash into *both* old_value and
            # new_value (the old hash being retired and the new one taking
            # its place) — an unredacted old_value would leak the very
            # secret a rotation is supposed to invalidate.
            audit_row = await pool.fetchrow(
                "SELECT * FROM audit_log WHERE entity_type = 'invite' AND entity_id = $1 "
                "AND action = 'updated' ORDER BY changed_at DESC LIMIT 1",
                row["id"],
            )
            assert audit_row is not None
            assert row["token_hash"] not in str(audit_row["old_value"])
            assert updated["token_hash"] not in str(audit_row["new_value"])
            assert "[redacted]" in audit_row["old_value"]
            assert "[redacted]" in audit_row["new_value"]

            with pytest.raises(LookupError):
                await invites.accept_invite(raw_token=old_token, password="a-real-password")

            user = await invites.accept_invite(raw_token=new_token, password="a-real-password")
            assert user["email"] == email
        finally:
            await _cleanup_invite(pool, row["id"])
            await _cleanup_user_by_email(pool, email)

    async def test_resend_within_60s_raises_resend_cooldown(self, scoped, pool, test_admin, test_tenant):
        # T18: the cooldown lives inside resend_invite's own locked
        # transaction (not a router-level check-then-act) so two
        # concurrent resends of the same row can't both slip past it
        # (lesson 8) — checked here by calling resend_invite twice back to
        # back, immediately after create_invite, whose own INSERT already
        # set last_sent_at = now().
        email = f"cooldown-{uuid.uuid4().hex[:8]}@example.com"
        row, _ = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            with pytest.raises(invites.ResendCooldown) as exc_info:
                await invites.resend_invite(row["id"], actor=_actor(test_admin["user"]))
            assert 0 < exc_info.value.remaining_seconds <= 60

            # Still resendable once the window has passed.
            await pool.execute(
                "UPDATE user_invites SET last_sent_at = now() - interval '61 seconds' WHERE id = $1",
                row["id"],
            )
            updated, _ = await invites.resend_invite(row["id"], actor=_actor(test_admin["user"]))
            assert updated["id"] == row["id"]
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_resend_404s_before_403s_on_missing_row(self, scoped, test_admin):
        with pytest.raises(LookupError):
            await invites.resend_invite(str(uuid.uuid4()), actor=_actor(test_admin["user"]))

    async def test_resend_of_another_tenants_invite_is_forbidden_not_404(
        self, scoped, pool, test_admin, test_tenant, test_superadmin,
    ):
        # Invite created by a superadmin in a *different* tenant than
        # test_admin's — an IDOR against the stored row, not the request.
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Test Other2", f"test-other2-{uuid.uuid4().hex[:8]}",
        )
        row, _ = await invites.create_invite(
            email=f"other-tenant-{uuid.uuid4().hex[:8]}@example.com", role="viewer",
            tenant_id=other["id"], team=None, actor=_actor(test_superadmin["user"]),
        )
        try:
            with pytest.raises(invites.PermissionDenied):
                await invites.resend_invite(row["id"], actor=_actor(test_admin["user"]))
            with pytest.raises(invites.PermissionDenied):
                await invites.revoke_invite(row["id"], actor=_actor(test_admin["user"]))
        finally:
            await _cleanup_invite(pool, row["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_revoke_blocks_accept(self, scoped, pool, test_admin, test_tenant):
        email = f"revoke-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await invites.revoke_invite(row["id"], actor=_actor(test_admin["user"]))
            with pytest.raises(invites.InviteRevoked):
                await invites.accept_invite(raw_token=raw_token, password="a-real-password")

            # AC13: revoke is a mutating action and must be audit-logged —
            # asserted directly against the row, not merely inferred from
            # revoke_invite calling write_audit unconditionally.
            audit_row = await pool.fetchrow(
                "SELECT * FROM audit_log WHERE entity_type = 'invite' AND entity_id = $1 "
                "AND action = 'updated' ORDER BY changed_at DESC LIMIT 1",
                row["id"],
            )
            assert audit_row is not None
            assert json.loads(audit_row["old_value"])["status"] == "pending"
            assert json.loads(audit_row["new_value"])["status"] == "revoked"
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_revoke_after_accept_does_not_touch_the_created_user(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # AC8: "revoking an accepted invite does not delete the resulting
        # user." revoke_invite's actual current behaviour on a non-pending
        # row is to raise InviteNotPending rather than transitioning it to
        # 'revoked' (verified separately — see 06-test-report.md's carried
        # note); whichever way that call responds, the one thing AC8
        # requires is that the user this invite already created is left
        # completely alone by the attempt. Would fail if revoke_invite (or
        # a future change to it) touched users.deleted_at or the password
        # hash for the invite's accepted_user_id.
        password = "a-real-password"
        email = f"revoke-after-accept-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            user = await invites.accept_invite(raw_token=raw_token, password=password)
            with pytest.raises(invites.InviteNotPending):
                await invites.revoke_invite(row["id"], actor=_actor(test_admin["user"]))

            still_there = await pool.fetchrow("SELECT * FROM users WHERE id = $1", user["id"])
            assert still_there is not None
            assert still_there["deleted_at"] is None
            assert await users_service.authenticate(email, password) is not None
        finally:
            await _cleanup_invite(pool, row["id"])
            await _cleanup_user_by_email(pool, email)


class TestAcceptInvite:
    async def test_creates_user_with_invites_role_tenant_team(self, scoped, pool, test_admin, test_tenant):
        email = f"accept-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team="ops",
            actor=_actor(test_admin["user"]),
        )
        try:
            user = await invites.accept_invite(raw_token=raw_token, password="a-real-password")
            assert user["role"] == "viewer"
            assert str(user["tenant_id"]) == str(test_tenant["id"])
            assert user["team"] == "ops"

            refreshed = await pool.fetchrow("SELECT * FROM user_invites WHERE id = $1", row["id"])
            assert refreshed["status"] == "accepted"
            assert refreshed["accepted_user_id"] == user["id"]

            # AC13: accept is a mutating action and must be audit-logged
            # like create/resend/revoke — asserted directly against the
            # row, not merely inferred from accept_invite calling
            # write_audit unconditionally.
            audit_row = await pool.fetchrow(
                "SELECT * FROM audit_log WHERE entity_type = 'invite' AND entity_id = $1 "
                "AND action = 'updated' ORDER BY changed_at DESC LIMIT 1",
                row["id"],
            )
            assert audit_row is not None
            assert audit_row["user_id"] == user["id"]
            assert json.loads(audit_row["new_value"])["status"] == "accepted"
        finally:
            await _cleanup_invite(pool, row["id"])
            await _cleanup_user_by_email(pool, email)

    async def test_expired_invite_raises_invite_expired(self, scoped, pool, test_admin, test_tenant):
        email = f"expired-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await pool.execute(
                "UPDATE user_invites SET expires_at = now() - interval '1 hour' WHERE id = $1", row["id"],
            )
            with pytest.raises(invites.InviteExpired):
                await invites.accept_invite(raw_token=raw_token, password="a-real-password")
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_replay_of_accepted_token_raises_invite_used_distinct_from_expired_and_revoked(
        self, scoped, pool, test_admin, test_tenant,
    ):
        email = f"replay-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await invites.accept_invite(raw_token=raw_token, password="a-real-password")
            with pytest.raises(invites.InviteUsed):
                await invites.accept_invite(raw_token=raw_token, password="a-real-password")
        finally:
            await _cleanup_invite(pool, row["id"])
            await _cleanup_user_by_email(pool, email)

    async def test_concurrent_accept_produces_exactly_one_user_and_one_invite_used(
        self, scoped, pool, test_admin, test_tenant,
    ):
        email = f"race-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            results = await asyncio.gather(
                invites.accept_invite(raw_token=raw_token, password="a-real-password"),
                invites.accept_invite(raw_token=raw_token, password="a-real-password"),
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, Exception)]
            failures = [r for r in results if isinstance(r, Exception)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], invites.InviteUsed)

            count = await pool.fetchval("SELECT count(*) FROM users WHERE lower(email) = lower($1)", email)
            assert count == 1
        finally:
            await _cleanup_invite(pool, row["id"])
            await _cleanup_user_by_email(pool, email)

    async def test_squatting_accept_raises_email_taken_and_leaves_invite_pending(
        self, scoped, pool, test_admin, test_tenant, test_superadmin,
    ):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Test Squat", f"test-squat-{uuid.uuid4().hex[:8]}",
        )
        email = f"squat-{uuid.uuid4().hex[:8]}@example.com"
        row_a, token_a = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        row_b, token_b = await invites.create_invite(
            email=email, role="admin", tenant_id=other["id"], team=None,
            actor=_actor(test_superadmin["user"]),
        )
        try:
            user = await invites.accept_invite(raw_token=token_a, password="a-real-password")
            assert user["email"] == email

            with pytest.raises(invites.EmailTaken):
                await invites.accept_invite(raw_token=token_b, password="another-password")

            refreshed_b = await pool.fetchrow("SELECT * FROM user_invites WHERE id = $1", row_b["id"])
            assert refreshed_b["status"] == "pending"
            # Still revocable/resendable, per the design's "not burned" guarantee.
            await invites.revoke_invite(row_b["id"], actor=_actor(test_superadmin["user"]))
        finally:
            await _cleanup_invite(pool, row_a["id"])
            await _cleanup_invite(pool, row_b["id"])
            await _cleanup_user_by_email(pool, email)
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_invite_stored_and_accepted_email_is_lower_cased_regardless_of_input_case(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # Design test plan, "Normalization": an invite stored from a
        # mixed-case address must resolve to one lower-cased user row, not
        # two distinct rows differing only by case. Would fail if
        # create_invite stopped lower-casing on the way in (row["email"]
        # would retain the mixed case) or if that stopped propagating
        # through to the created user.
        mixed_email = f"CASE-{uuid.uuid4().hex[:8]}@Example.COM"
        row, raw_token = await invites.create_invite(
            email=mixed_email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            assert row["email"] == mixed_email.lower()
            user = await invites.accept_invite(raw_token=raw_token, password="a-real-password")
            assert user["email"] == mixed_email.lower()
            count = await pool.fetchval(
                "SELECT count(*) FROM users WHERE lower(email) = lower($1)", mixed_email,
            )
            assert count == 1
        finally:
            await _cleanup_invite(pool, row["id"])
            await _cleanup_user_by_email(pool, mixed_email)

    async def test_inviter_soft_deleted_before_accept_raises_invite_context_gone(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # Security finding: de-provisioning the inviter must de-provision
        # what they already granted, up to 7 days later.
        email = f"gone-inviter-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await pool.execute(
                "UPDATE users SET deleted_at = now() WHERE id = $1", test_admin["user"]["id"],
            )
            with pytest.raises(invites.InviteContextGone):
                await invites.accept_invite(raw_token=raw_token, password="a-real-password")

            # Rolled back, not burned: still pending, and no user was created.
            refreshed = await pool.fetchrow("SELECT * FROM user_invites WHERE id = $1", row["id"])
            assert refreshed["status"] == "pending"
            count = await pool.fetchval(
                "SELECT count(*) FROM users WHERE lower(email) = lower($1)", email,
            )
            assert count == 0
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_inviter_demoted_before_accept_raises_invite_context_gone(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # Inviter still exists and isn't soft-deleted, but no longer holds a
        # role that could have issued this exact grant — re-run may_invite
        # against their CURRENT role, not what was true when they sent it.
        email = f"demoted-inviter-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await pool.execute(
                "UPDATE users SET role = 'viewer' WHERE id = $1", test_admin["user"]["id"],
            )
            with pytest.raises(invites.InviteContextGone):
                await invites.accept_invite(raw_token=raw_token, password="a-real-password")
        finally:
            await _cleanup_invite(pool, row["id"])
            await pool.execute("UPDATE users SET role = 'admin' WHERE id = $1", test_admin["user"]["id"])

    async def test_target_tenant_soft_deleted_before_accept_raises_invite_context_gone(
        self, scoped, pool, test_admin, test_tenant,
    ):
        email = f"gone-tenant-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await pool.execute("UPDATE tenants SET deleted_at = now() WHERE id = $1", test_tenant["id"])
            with pytest.raises(invites.InviteContextGone):
                await invites.accept_invite(raw_token=raw_token, password="a-real-password")
        finally:
            await _cleanup_invite(pool, row["id"])


class TestGetInviteForAccept:
    """PR #19 finding 3: get_invite_for_accept used to classify only
    revoked/accepted/expired and skip the granting-context re-validation
    accept_invite performs, so GET kept returning a live-looking invite for
    the full 7-day TTL after the inviter (or their tenant) was soft-deleted
    — while POST already correctly 410s. GET and POST must agree. Mutation-
    verified: removing the `await _check_context_live(pool, invite)` call
    added to get_invite_for_accept makes each of these fail (the call
    returns a dict instead of raising)."""

    async def test_inviter_soft_deleted_makes_get_raise_context_gone(
        self, scoped, pool, test_admin, test_tenant,
    ):
        email = f"gone-inviter-get-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await pool.execute(
                "UPDATE users SET deleted_at = now() WHERE id = $1", test_admin["user"]["id"],
            )
            with pytest.raises(invites.InviteContextGone):
                await invites.get_invite_for_accept(raw_token=raw_token)
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_target_tenant_soft_deleted_makes_get_raise_context_gone(
        self, scoped, pool, test_admin, test_tenant,
    ):
        email = f"gone-tenant-get-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            await pool.execute("UPDATE tenants SET deleted_at = now() WHERE id = $1", test_tenant["id"])
            with pytest.raises(invites.InviteContextGone):
                await invites.get_invite_for_accept(raw_token=raw_token)
        finally:
            await _cleanup_invite(pool, row["id"])

    async def test_still_live_invite_is_returned_by_get(self, scoped, pool, test_admin, test_tenant):
        # Control: a live invite's granting context is unaffected, so GET
        # must still return normally — proves the check above isn't
        # unconditionally raising.
        email = f"live-get-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            result = await invites.get_invite_for_accept(raw_token=raw_token)
            assert result["email"] == email
            assert result["status"] == "pending"
        finally:
            await _cleanup_invite(pool, row["id"])


class TestSoftDeleteReinvite:
    async def test_reinvite_after_soft_delete_creates_second_live_row_and_retires_old_password(
        self, scoped, pool, test_admin, test_tenant,
    ):
        # T23 / design "Soft-delete re-invite (finding 4)": invite -> accept
        # -> soft-delete the resulting user -> re-invite the SAME address ->
        # accept again. This must produce a second live user row (not a
        # conflict, not a resurrection of the first row), and the first
        # row's original password must stop authenticating once it is
        # soft-deleted. Would fail if users_email_lower_idx regressed from
        # a partial (`WHERE deleted_at IS NULL`) index to a plain unique
        # index on lower(email) — the second create_invite/accept would
        # raise EmailConflict/EmailTaken instead of succeeding — or if
        # authenticate() stopped filtering deleted_at, letting the retired
        # password keep working.
        email = f"reinvite-{uuid.uuid4().hex[:8]}@example.com"
        first_password = "first-real-password"
        second_password = "second-real-password"

        row_1, token_1 = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        user_1 = await invites.accept_invite(raw_token=token_1, password=first_password)
        await users_service.soft_delete_user(user_1["id"])

        row_2, token_2 = await invites.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            user_2 = await invites.accept_invite(raw_token=token_2, password=second_password)
            assert user_2["id"] != user_1["id"]

            live_count = await pool.fetchval(
                "SELECT count(*) FROM users WHERE lower(email) = lower($1) AND deleted_at IS NULL",
                email,
            )
            assert live_count == 1

            assert await users_service.authenticate(email, first_password) is None
            assert await users_service.authenticate(email, second_password) is not None
        finally:
            await _cleanup_invite(pool, row_1["id"])
            await _cleanup_invite(pool, row_2["id"])
            await _cleanup_user_by_email(pool, email)
