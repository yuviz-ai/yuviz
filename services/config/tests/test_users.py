from __future__ import annotations

import uuid

import pytest

from services.config import users


async def test_create_and_get_user_by_email(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="s3cret-pw", role="admin")
    assert created["email"] == email
    assert created["role"] == "admin"
    assert created["password_hash"] != "s3cret-pw"  # never stored in plaintext

    fetched = await users.get_user_by_email(email)
    assert fetched is not None
    assert fetched["id"] == created["id"]

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_authenticate_succeeds_with_correct_password(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="correct-pw", role="admin")

    authenticated = await users.authenticate(email, "correct-pw")
    assert authenticated is not None
    assert authenticated["id"] == created["id"]

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_authenticate_fails_with_wrong_password(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="correct-pw", role="admin")

    assert await users.authenticate(email, "wrong-pw") is None

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_authenticate_fails_for_unknown_email():
    assert await users.authenticate("no-such-user@example.com", "anything") is None


async def test_to_public_dict_strips_password_hash(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="pw", role="admin")

    public = users.to_public_dict(created)
    assert "password_hash" not in public
    assert public["email"] == email

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_update_user_role(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="pw", role="viewer")

    updated = await users.update_user(created["id"], role="admin")
    assert updated["role"] == "admin"

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_update_user_rejects_unknown_field(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="pw", role="admin")

    with pytest.raises(ValueError):
        await users.update_user(created["id"], password_hash="bypass-attempt")

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_soft_delete_user_excluded_from_get_and_authenticate(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="pw", role="admin")

    await users.soft_delete_user(created["id"])

    assert await users.get_user_by_email(email) is None
    assert await users.authenticate(email, "pw") is None

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_duplicate_email_rejected(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="pw", role="admin")

    with pytest.raises(Exception):
        await users.create_user(email=email, password="different-pw", role="viewer")

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_change_password_succeeds_and_new_password_works(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="old-password", role="admin")

    # platform_scoped=True: this user has tenant_id=None (a platform actor,
    # deps.is_platform_scoped's own definition), matching how the real
    # /auth/change-password route would call this for such a caller.
    ok = await users.change_password(
        created["id"], current_password="old-password", new_password="new-password", platform_scoped=True,
    )
    assert ok is True

    assert await users.authenticate(email, "old-password") is None
    assert await users.authenticate(email, "new-password") is not None

    await pool.execute("UPDATE users SET deleted_at = now() WHERE id = $1", created["id"])


async def test_change_password_fails_with_wrong_current_password(pool):
    email = f"test-user-{uuid.uuid4().hex[:8]}@example.com"
    created = await users.create_user(email=email, password="old-password", role="admin")

    ok = await users.change_password(
        created["id"], current_password="totally-wrong", new_password="new-password", platform_scoped=True,
    )
    assert ok is False

    # Old password still works — a failed attempt must not touch the hash.
    assert await users.authenticate(email, "old-password") is not None

    await pool.execute("DELETE FROM users WHERE id = $1", created["id"])
