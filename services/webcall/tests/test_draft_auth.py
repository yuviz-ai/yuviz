"""Webcall session auth gates (every session, not only ?draft=1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from services.webcall.__main__ import (
    _CONSOLE_ROLES,
    _await_session_auth,
    _config_tenant_check,
    _session_auth_problem,
)

SECRET = "dev-only-insecure-secret-do-not-deploy-dev-only-insecure-secret-do-not-deploy-"


def _tok(**extra) -> str:
    payload = {
        "sub": "u1",
        "email": "a@b.c",
        "role": "admin",
        "tenant_id": "t-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "is_service_account": False,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        **extra,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)


@pytest.mark.asyncio
async def test_console_roles_only():
    assert _CONSOLE_ROLES == frozenset({"superadmin", "admin", "viewer"})
    with patch(
        "services.webcall.__main__._config_tenant_check", new_callable=AsyncMock,
    ) as check:
        check.return_value = None
        for role in ("admin", "viewer", "superadmin"):
            err, user_id = await _session_auth_problem(_tok(role=role), "acme")
            assert err is None and user_id == "u1"
        for role in ("agent", "supervisor"):
            err, user_id = await _session_auth_problem(_tok(role=role), "acme")
            assert err and "not allowed" in err and user_id is None


@pytest.mark.asyncio
async def test_rejects_when_config_denies_tenant():
    token = _tok()
    with patch(
        "services.webcall.__main__._config_tenant_check", new_callable=AsyncMock,
    ) as check:
        check.return_value = "session is not allowed for this tenant"
        err, user_id = await _session_auth_problem(token, "other-tenant")
        assert err and "tenant" in err and user_id is None
        check.assert_awaited_once_with(token, "other-tenant")


@pytest.mark.asyncio
async def test_rejects_missing_and_service_account():
    err, user_id = await _session_auth_problem(None, "acme")
    assert err and user_id is None
    err, user_id = await _session_auth_problem(_tok(is_service_account=True), "acme")
    assert err and user_id is None


@pytest.mark.asyncio
async def test_rejects_missing_sub():
    with patch(
        "services.webcall.__main__._config_tenant_check", new_callable=AsyncMock,
    ) as check:
        check.return_value = None
        err, user_id = await _session_auth_problem(_tok(sub=""), "acme")
        assert err and "user id" in err and user_id is None


@pytest.mark.asyncio
async def test_config_tenant_check_distinguishes_transport_from_forbidden(monkeypatch):
    monkeypatch.setenv("CONFIG_SERVICE_URL", "http://config.test")

    with patch("services.webcall.__main__.asyncio.to_thread", new_callable=AsyncMock) as thr:
        thr.return_value = 404
        err = await _config_tenant_check(_tok(), "other")
        assert err and "not allowed" in err

        thr.return_value = None  # transport failure
        err = await _config_tenant_check(_tok(), "acme")
        assert err and "could not verify" in err

        thr.return_value = 200
        assert await _config_tenant_check(_tok(), "acme") is None

        thr.return_value = 503
        err = await _config_tenant_check(_tok(), "acme")
        assert err and "could not verify" in err


class _FakeWs:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[str] = []

    async def recv(self):
        if not self._frames:
            raise TimeoutError("no frames")
        return self._frames.pop(0)

    async def send(self, data: str):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_await_session_auth_happy_path():
    token = _tok()
    ws = _FakeWs([json.dumps({"type": "auth", "token": token})])
    with patch(
        "services.webcall.__main__._config_tenant_check", new_callable=AsyncMock,
    ) as check:
        check.return_value = None
        err, user_id = await _await_session_auth(ws, "acme")
        assert err is None and user_id == "u1"
    assert json.loads(ws.sent[0]) == {"type": "auth_ok"}


@pytest.mark.asyncio
async def test_await_session_auth_rejects_bad_frames():
    import asyncio

    ws = MagicMock()
    with patch(
        "services.webcall.__main__.asyncio.wait_for",
        side_effect=asyncio.TimeoutError,
    ):
        err, user_id = await _await_session_auth(ws, "acme")
        assert err and "timed out" in err and user_id is None

    ws = _FakeWs([b"\x00\x01"])
    err, user_id = await _await_session_auth(ws, "acme")
    assert err and "text frame" in err and user_id is None

    ws = _FakeWs(["not-json{"])
    err, user_id = await _await_session_auth(ws, "acme")
    assert err and "not JSON" in err and user_id is None

    ws = _FakeWs([json.dumps({"type": "text_input", "text": "hi"})])
    err, user_id = await _await_session_auth(ws, "acme")
    assert err and "auth frame first" in err and user_id is None
