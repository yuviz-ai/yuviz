"""HTTP-level invite routes and their in-process throttles.

Throttle buckets live on the shared app and aren't reset, so tests use fresh fake client IPs.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_target_tenant
from services.config import invites as invites_service
from services.config import users as users_service
from services.config.app import app
from services.config.auth import CurrentUser


def _client(token: str | None = None, *, host: str = "127.0.0.1"):
    transport = ASGITransport(app=app, client=(host, 123))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


def _actor(user: dict) -> CurrentUser:
    return CurrentUser(
        id=str(user["id"]), email=user["email"], role=user["role"],
        tenant_id=str(user["tenant_id"]) if user["tenant_id"] is not None else None,
    )


def _fake_host() -> str:
    return f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"


@pytest.fixture
async def admin_client(test_admin):
    async with _client(test_admin["token"]) as c:
        yield c


@pytest.fixture
async def superadmin_client(test_superadmin):
    async with _client(test_superadmin["token"]) as c:
        yield c


@pytest.fixture
async def viewer_client(test_viewer):
    async with _client(test_viewer["token"]) as c:
        yield c


async def _create_invite(client, *, email=None, role="viewer", tenant_id=None, team=None):
    email = email or f"invite-{uuid.uuid4().hex[:8]}@example.com"
    body = {"email": email, "role": role, "team": team}
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    return await client.post("/invites", json=body)


@pytest.fixture(autouse=True)
def _smtp_succeeds():
    # No real SMTP; tests that need a failure patch it themselves.
    with patch("services.config.email.send_invite_email", return_value=None):
        yield


class TestScoping:
    async def test_tenant_id_filter_is_forced_for_tenant_admin(self, admin_client, test_admin, test_tenant, pool):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Other Scoping Tenant", f"test-other-{uuid.uuid4().hex[:8]}",
        )
        try:
            resp = await _create_invite(admin_client, tenant_id=test_tenant["id"])
            assert resp.status_code == 201
            invite_id = resp.json()["id"]

            # A foreign ?tenant_id= is ignored, so both lists match.
            own = await admin_client.get("/invites")
            forced = await admin_client.get(f"/invites?tenant_id={other['id']}")
            assert {row["id"] for row in own.json()} == {row["id"] for row in forced.json()}
            assert invite_id in {row["id"] for row in own.json()}
            assert all(row["tenant_id"] == str(test_tenant["id"]) for row in own.json())
        finally:
            await pool.execute("DELETE FROM user_invites WHERE tenant_id IN ($1, $2)", test_tenant["id"], other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_superadmin_sees_across_tenants_unfiltered(
        self, superadmin_client, admin_client, test_tenant, test_superadmin, pool,
    ):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "Unfiltered Tenant", f"test-unfiltered-{uuid.uuid4().hex[:8]}",
        )
        resp = await _create_invite(admin_client, tenant_id=test_tenant["id"])
        assert resp.status_code == 201
        own_invite_id = resp.json()["id"]
        set_target_tenant(str(other["id"]))
        try:
            other_row, _ = await invites_service.create_invite(
                email=f"other-tenant-invite-{uuid.uuid4().hex[:8]}@example.com", role="viewer",
                tenant_id=other["id"], team=None,
                actor=_actor(test_superadmin["user"]),
            )
        finally:
            set_target_tenant(None)
        try:
            resp = await superadmin_client.get("/invites")
            assert resp.status_code == 200
            ids = {row["id"] for row in resp.json()}
            assert own_invite_id in ids
            assert str(other_row["id"]) in ids
        finally:
            await pool.execute("DELETE FROM user_invites WHERE id = $1", own_invite_id)
            await pool.execute("DELETE FROM user_invites WHERE id = $1", other_row["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])

    async def test_idor_resend_and_revoke_of_another_tenants_invite_is_403(
        self, pool, test_admin, test_tenant,
    ):
        other = await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *",
            "IDOR Tenant", f"test-idor-{uuid.uuid4().hex[:8]}",
        )
        other_admin_email = f"other-admin-{uuid.uuid4().hex[:8]}@example.com"
        other_admin = await users_service.create_user(
            email=other_admin_email, password="a-real-password", role="admin", tenant_id=other["id"],
        )
        set_target_tenant(str(other["id"]))
        try:
            row, _ = await invites_service.create_invite(
                email=f"target-{uuid.uuid4().hex[:8]}@example.com", role="viewer",
                tenant_id=other["id"], team=None,
                actor=_actor(other_admin),
            )
        finally:
            set_target_tenant(None)
        try:
            async with _client(test_admin["token"]) as attacker:
                resend_resp = await attacker.post(f"/invites/{row['id']}/resend")
                assert resend_resp.status_code == 403
                revoke_resp = await attacker.post(f"/invites/{row['id']}/revoke")
                assert revoke_resp.status_code == 403
        finally:
            await pool.execute("DELETE FROM user_invites WHERE id = $1", row["id"])
            # Soft-delete: other_admin is an audit_log actor.
            await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", other_admin["id"])
            await pool.execute("UPDATE users SET tenant_id = NULL WHERE tenant_id = $1", other["id"])
            await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


class TestCreateAndDelivery:
    async def test_create_returns_201_with_email_sent_true(self, admin_client, test_tenant, pool):
        resp = await _create_invite(admin_client, tenant_id=test_tenant["id"])
        assert resp.status_code == 201
        body = resp.json()
        assert body["email_sent"] is True
        assert "token_hash" not in body
        await pool.execute("DELETE FROM user_invites WHERE id = $1", body["id"])

    async def test_smtp_failure_is_non_fatal_and_row_stays_pending_and_resendable(
        self, admin_client, test_tenant, pool,
    ):
        with patch("services.config.email.send_invite_email", side_effect=RuntimeError("smtp down")):
            resp = await _create_invite(admin_client, tenant_id=test_tenant["id"])
        assert resp.status_code == 201
        body = resp.json()
        assert body["email_sent"] is False
        assert body["status"] == "pending"

        # Back-date last_sent_at past the 60s resend cooldown.
        await pool.execute(
            "UPDATE user_invites SET last_sent_at = now() - interval '61 seconds' WHERE id = $1",
            body["id"],
        )
        # Non-fatal at resend too, and the row is still resendable.
        with patch("services.config.email.send_invite_email", side_effect=RuntimeError("smtp still down")):
            resend_resp = await admin_client.post(f"/invites/{body['id']}/resend")
        assert resend_resp.status_code == 200
        assert resend_resp.json()["email_sent"] is False
        assert resend_resp.json()["status"] == "pending"

        await pool.execute("DELETE FROM user_invites WHERE id = $1", body["id"])

    async def test_viewer_cannot_create_invite(self, viewer_client, test_tenant):
        resp = await _create_invite(viewer_client, tenant_id=test_tenant["id"])
        assert resp.status_code == 403


class TestAcceptRoutesArePublic:
    async def test_get_and_post_accept_reach_handlers_with_no_authorization_header(self):
        async with _client(host=_fake_host()) as anon:
            get_resp = await anon.get("/invites/accept", headers={"X-Invite-Token": "not-a-real-token"})
            post_resp = await anon.post(
                "/invites/accept", json={"password": "a-real-password"},
                headers={"X-Invite-Token": "not-a-real-token"},
            )
        # 404 (bad token), not 401/403 — proving the console gate never ran.
        assert get_resp.status_code == 404
        assert post_resp.status_code == 404

    async def test_get_accept_returns_only_invitee_shape(self, test_admin, test_tenant, scoped, pool):
        # The HTTP response never carries the raw token, so create via the service.
        email = f"accept-shape-{uuid.uuid4().hex[:8]}@example.com"
        row, raw_token = await invites_service.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            async with _client(host=_fake_host()) as anon:
                get_resp = await anon.get("/invites/accept", headers={"X-Invite-Token": raw_token})
            assert get_resp.status_code == 200
            body = get_resp.json()
            assert set(body.keys()) == {"email", "tenant_name", "role", "status"}
            assert body["email"] == email
        finally:
            await pool.execute("DELETE FROM user_invites WHERE id = $1", row["id"])


class TestProbeThrottle:
    async def test_31st_create_attempt_is_429_and_outcome_blind(self, test_admin, test_tenant, scoped, pool):
        # Seeded via the service so it doesn't count against the probe quota.
        email = f"probe-{uuid.uuid4().hex[:8]}@example.com"
        seed_row, _ = await invites_service.create_invite(
            email=email, role="viewer", tenant_id=test_tenant["id"], team=None,
            actor=_actor(test_admin["user"]),
        )
        try:
            async with _client(test_admin["token"], host=_fake_host()) as c:
                # 30 409s, each still counted (outcome-blind).
                for _ in range(30):
                    resp = await _create_invite(c, email=email, tenant_id=test_tenant["id"])
                    assert resp.status_code == 409
                # A fresh email is still blocked: the limit is per attempt, not per email.
                blocked = await _create_invite(
                    c, email=f"fresh-{uuid.uuid4().hex[:8]}@example.com", tenant_id=test_tenant["id"],
                )
                assert blocked.status_code == 429
                assert "Retry-After" in blocked.headers
        finally:
            await pool.execute("DELETE FROM user_invites WHERE id = $1", seed_row["id"])


class TestSendCap:
    async def test_21st_successful_send_is_429(self, test_admin, test_tenant, pool):
        async with _client(test_admin["token"], host=_fake_host()) as c:
            created_ids = []
            try:
                for _ in range(20):
                    resp = await _create_invite(c, tenant_id=test_tenant["id"])
                    assert resp.status_code == 201
                    created_ids.append(resp.json()["id"])
                resp = await _create_invite(c, tenant_id=test_tenant["id"])
                assert resp.status_code == 429
            finally:
                for invite_id in created_ids:
                    await pool.execute("DELETE FROM user_invites WHERE id = $1", invite_id)


class TestResendCooldown:
    async def test_second_resend_within_60s_is_429_naming_remaining_seconds(
        self, admin_client, test_tenant, pool,
    ):
        resp = await _create_invite(admin_client, tenant_id=test_tenant["id"])
        invite_id = resp.json()["id"]
        try:
            # Back-date so the first resend succeeds; the second is the one under test.
            await pool.execute(
                "UPDATE user_invites SET last_sent_at = now() - interval '61 seconds' WHERE id = $1",
                invite_id,
            )
            first = await admin_client.post(f"/invites/{invite_id}/resend")
            assert first.status_code == 200
            second = await admin_client.post(f"/invites/{invite_id}/resend")
            assert second.status_code == 429
            assert "s" in second.json()["detail"]
            assert "Retry-After" in second.headers
        finally:
            await pool.execute("DELETE FROM user_invites WHERE id = $1", invite_id)


class TestAcceptIpThrottle:
    async def test_11th_accept_request_in_a_minute_is_429_regardless_of_token_validity(
        self, test_admin, test_tenant, scoped, pool,
    ):
        host = _fake_host()
        async with _client(host=host) as c:
            for _ in range(10):
                resp = await c.get("/invites/accept", headers={"X-Invite-Token": "bogus"})
                assert resp.status_code == 404
            blocked_invalid = await c.get("/invites/accept", headers={"X-Invite-Token": "bogus"})
            assert blocked_invalid.status_code == 429
            invalid_body = blocked_invalid.json()

        # Fresh IP with a valid token: the 429 must not reveal whether the token was real.
        row, raw_token = await invites_service.create_invite(
            email=f"throttle-valid-{uuid.uuid4().hex[:8]}@example.com", role="viewer",
            tenant_id=test_tenant["id"], team=None, actor=_actor(test_admin["user"]),
        )
        try:
            async with _client(host=_fake_host()) as c:
                for _ in range(10):
                    resp = await c.get("/invites/accept", headers={"X-Invite-Token": raw_token})
                    assert resp.status_code == 200
                blocked_valid = await c.get("/invites/accept", headers={"X-Invite-Token": raw_token})
                assert blocked_valid.status_code == 429
                assert blocked_valid.json() == invalid_body
        finally:
            await pool.execute("DELETE FROM user_invites WHERE id = $1", row["id"])

    async def test_forged_x_forwarded_for_does_not_change_the_bucket(self):
        host = _fake_host()
        async with _client(host=host) as c:
            for _ in range(10):
                await c.get("/invites/accept", headers={"X-Invite-Token": "bogus"})
            # A forged XFF must not move the request into a fresh bucket.
            resp = await c.get(
                "/invites/accept",
                headers={"X-Invite-Token": "bogus", "X-Forwarded-For": _fake_host()},
            )
            assert resp.status_code == 429

    async def test_no_client_in_scope_still_throttles_instead_of_500ing(self):
        # request.client can be None (e.g. Unix socket); must neither 500 nor skip the throttle.
        transport = ASGITransport(app=app, client=None)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            for _ in range(10):
                resp = await c.get("/invites/accept", headers={"X-Invite-Token": "bogus"})
                assert resp.status_code == 404
            blocked = await c.get("/invites/accept", headers={"X-Invite-Token": "bogus"})
            assert blocked.status_code == 429
