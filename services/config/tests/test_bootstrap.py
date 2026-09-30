"""
First-run setup: /auth/setup-status + /auth/bootstrap.

These cannot use the shared 'voiceai' database — "zero superadmins" is a
property of the whole database, and conftest's fixtures put superadmins in
it. Each test gets its own throwaway database instead, installed as db.py's
process-wide pool so the code under test needs no changes.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import auth, db
from services.config import users as users_service
from services.config.app import app

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_SQL = (REPO_ROOT / "database" / "schema.sql").read_text()


@pytest_asyncio.fixture(loop_scope="session")
async def fresh_db():
    """An empty, schema-applied database installed as db.py's process-wide
    pool; the original pool is restored afterwards."""
    base_dsn = os.environ["POSTGRES_DSN"]
    name = f"yuviz_bootstrap_test_{uuid.uuid4().hex[:12]}"

    # CREATE DATABASE can't run in a transaction — standalone connection.
    conn = await asyncpg.connect(base_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()

    test_dsn = urlunsplit(urlsplit(base_dsn)._replace(path=f"/{name}"))
    test_pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=10)
    async with test_pool.acquire() as c:
        await c.execute(SCHEMA_SQL)

    original = db._pool  # noqa: SLF001
    db._pool = test_pool  # noqa: SLF001
    try:
        yield test_pool
    finally:
        db._pool = original  # noqa: SLF001
        await test_pool.close()
        conn = await asyncpg.connect(base_dsn)
        try:
            await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await conn.close()


@pytest_asyncio.fixture(loop_scope="session")
async def anon_client(fresh_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestSetupStatus:
    async def test_fresh_database_requires_setup(self, anon_client):
        resp = await anon_client.get("/auth/setup-status")
        assert resp.status_code == 200
        assert resp.json() == {"setup_required": True}

    async def test_setup_not_required_once_a_superadmin_exists(self, anon_client):
        await anon_client.post(
            "/auth/bootstrap", json={"email": "first@example.com", "password": "a-real-password"},
        )
        resp = await anon_client.get("/auth/setup-status")
        assert resp.json() == {"setup_required": False}

    async def test_non_superadmin_users_do_not_count_as_setup(self, fresh_db, anon_client):
        # init.sh creates this viewer before anyone opens the UI.
        await users_service.create_user(
            email="service@internal.example.com", password="service-password", role="viewer",
        )
        resp = await anon_client.get("/auth/setup-status")
        assert resp.json() == {"setup_required": True}


class TestBootstrap:
    async def test_creates_first_superadmin_and_returns_a_usable_token(self, anon_client):
        resp = await anon_client.post(
            "/auth/bootstrap", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["user"]["email"] == "admin@example.com"
        assert body["user"]["role"] == "superadmin"
        assert body["user"]["tenant_id"] is None
        assert body["token_type"] == "bearer"

        me = await anon_client.get(
            "/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["email"] == "admin@example.com"

    async def test_response_never_exposes_password_hash(self, anon_client):
        resp = await anon_client.post(
            "/auth/bootstrap", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        assert "password_hash" not in resp.json()["user"]
        assert "a-real-password" not in resp.text

    async def test_password_is_bcrypt_hashed_not_stored_plaintext(self, fresh_db, anon_client):
        await anon_client.post(
            "/auth/bootstrap", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        stored = await fresh_db.fetchval("SELECT password_hash FROM users WHERE email = $1", "admin@example.com")
        assert stored != "a-real-password"
        assert stored.startswith("$2b$")
        assert auth.verify_password("a-real-password", stored)

    async def test_second_bootstrap_is_rejected(self, anon_client):
        first = await anon_client.post(
            "/auth/bootstrap", json={"email": "first@example.com", "password": "a-real-password"},
        )
        assert first.status_code == 201
        second = await anon_client.post(
            "/auth/bootstrap", json={"email": "second@example.com", "password": "another-password"},
        )
        assert second.status_code == 409

    async def test_second_bootstrap_writes_nothing(self, fresh_db, anon_client):
        await anon_client.post(
            "/auth/bootstrap", json={"email": "first@example.com", "password": "a-real-password"},
        )
        await anon_client.post(
            "/auth/bootstrap", json={"email": "second@example.com", "password": "another-password"},
        )
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"email": "not-an-email", "password": "a-real-password"},
            {"email": "admin@example.com", "password": "short"},
            {"email": "admin@example.com"},
        ],
        ids=["malformed-email", "password-too-short", "missing-password"],
    )
    async def test_backend_rejects_invalid_input(self, fresh_db, anon_client, body):
        resp = await anon_client.post("/auth/bootstrap", json=body)
        assert resp.status_code == 422
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 0

    async def test_email_already_taken_by_a_non_superadmin(self, anon_client):
        await users_service.create_user(
            email="taken@example.com", password="service-password", role="viewer",
        )
        resp = await anon_client.post(
            "/auth/bootstrap", json={"email": "taken@example.com", "password": "a-real-password"},
        )
        assert resp.status_code == 409


class TestBootstrapConcurrency:
    async def test_simultaneous_attempts_create_exactly_one_superadmin(self, fresh_db):
        """Without the advisory lock all ten would read "no superadmin"
        before any of them inserted, and all ten would succeed."""
        results = await asyncio.gather(*(
            users_service.bootstrap_first_superadmin(
                email=f"racer{i}@example.com", password="a-real-password",
            )
            for i in range(10)
        ))
        created = [r for r in results if r is not None]
        assert len(created) == 1
        assert created[0]["role"] == "superadmin"
        assert await fresh_db.fetchval("SELECT count(*) FROM users") == 1


class TestPostBootstrapBehaviorIsUnchanged:
    async def test_normal_login_works_after_bootstrap(self, anon_client):
        await anon_client.post(
            "/auth/bootstrap", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        resp = await anon_client.post(
            "/auth/login", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "superadmin"
        assert "password_hash" not in resp.json()["user"]

    async def test_login_still_hides_whether_an_email_exists(self, anon_client):
        await anon_client.post(
            "/auth/bootstrap", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        wrong_password = await anon_client.post(
            "/auth/login", json={"email": "admin@example.com", "password": "wrong-password"},
        )
        unknown_email = await anon_client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"},
        )
        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json()["detail"] == unknown_email.json()["detail"]

    async def test_the_bootstrapped_superadmin_can_still_onboard_users(self, anon_client):
        # POST /users (the temp-password path) is gone as of Phase 3 — the
        # only account-creation path left is invite -> accept.
        created = await anon_client.post(
            "/auth/bootstrap", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        headers = {"Authorization": f"Bearer {created.json()['access_token']}"}

        with patch("services.config.email.send_invite_email") as mock_send:
            resp = await anon_client.post(
                "/invites", json={"email": "colleague@example.com", "role": "admin"}, headers=headers,
            )
        assert resp.status_code == 201
        raw_token = mock_send.call_args.kwargs["raw_token"]

        accept_resp = await anon_client.post(
            "/invites/accept", json={"password": "another-password"},
            headers={"X-Invite-Token": raw_token},
        )
        assert accept_resp.status_code == 200
        assert accept_resp.json()["role"] == "admin"

        listed = await anon_client.get("/users", headers=headers)
        assert {u["email"] for u in listed.json()} == {"admin@example.com", "colleague@example.com"}

    async def test_inviting_a_second_superadmin_still_works(self, anon_client):
        """The guard must not have become a one-superadmin constraint."""
        created = await anon_client.post(
            "/auth/bootstrap", json={"email": "admin@example.com", "password": "a-real-password"},
        )
        headers = {"Authorization": f"Bearer {created.json()['access_token']}"}
        with patch("services.config.email.send_invite_email") as mock_send:
            resp = await anon_client.post(
                "/invites", json={"email": "second-admin@example.com", "role": "superadmin"}, headers=headers,
            )
        assert resp.status_code == 201
        raw_token = mock_send.call_args.kwargs["raw_token"]

        accept_resp = await anon_client.post(
            "/invites/accept", json={"password": "another-password"},
            headers={"X-Invite-Token": raw_token},
        )
        assert accept_resp.status_code == 200
        assert accept_resp.json()["role"] == "superadmin"


class TestNoDefaultCredentialsRemain:
    """A grep, as a test — tracked files only."""

    @pytest.mark.parametrize("needle", ["admin@yuviz.local", "admin123", "ADMIN_EMAIL", "ADMIN_PASSWORD"])
    def test_no_references_in_tracked_files(self, needle):
        files = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.split("\0")
        hits = []
        for name in filter(None, files):
            path = REPO_ROOT / name
            # This file names the strings it forbids; skip itself.
            if path == Path(__file__) or not path.is_file():
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if needle in text:
                hits.append(name)
        assert hits == [], f"{needle!r} still referenced in: {hits}"
