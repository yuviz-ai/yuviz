"""send_invite_email(): bounded timeouts, off-event-loop sends, and mandatory verified STARTTLS."""

from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.config import email
from services.config.app import app, lifespan


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "invites@example.com")
    monkeypatch.setenv("SMTP_FROM", "invites@example.com")
    monkeypatch.setenv("SMTP_PASSWORD_REF", "env:SMTP_PASSWORD")
    monkeypatch.setenv("SMTP_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("INVITE_BASE_URL", "http://localhost:3000")


async def test_smtp_connection_is_made_with_an_explicit_timeout():
    # Without timeout=, smtplib.SMTP() blocks forever.
    with patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = MagicMock()
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    args, kwargs = mock_smtp.call_args
    assert kwargs.get("timeout") == 10 or (len(args) >= 3 and args[2] == 10)


async def test_send_invite_email_does_not_block_the_event_loop():
    # A slow SMTP call runs off-thread, so a concurrent coroutine finishes first.
    order: list[str] = []

    def _slow_smtp(*args, **kwargs):
        time.sleep(0.3)
        order.append("smtp")
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock()
        cm.__exit__.return_value = False
        return cm

    async def _fast_task():
        await asyncio.sleep(0.05)
        order.append("fast")

    with patch("smtplib.SMTP", side_effect=_slow_smtp):
        await asyncio.gather(
            email.send_invite_email(to_email="new-user@example.com", raw_token="tok"),
            _fast_task(),
        )

    assert order == ["fast", "smtp"]


async def test_smtp_failure_raises_and_does_not_hang(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "203.0.113.1")  # TEST-NET-3, guaranteed unroutable
    with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        with pytest.raises(OSError):
            await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")


async def test_send_is_bounded_even_if_every_socket_op_stalls(monkeypatch):
    # timeout= bounds each socket op only; the outer wait_for bounds the whole send.
    monkeypatch.setattr(email, "_SMTP_TIMEOUT_SECONDS", 0.2)

    def _hangs_forever(*args, **kwargs):
        time.sleep(5)

    with patch("smtplib.SMTP", side_effect=_hangs_forever):
        start = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")
        assert time.monotonic() - start < 1.0


async def test_starttls_is_called_by_default():
    # STARTTLS is on by default: the password and invite token are bearer secrets.
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.starttls.assert_called_once()
    # starttls must precede login, or the credential still goes out in cleartext.
    call_names = [call_obj[0] for call_obj in smtp_instance.method_calls]
    assert call_names.index("starttls") < call_names.index("login")


async def test_starttls_uses_a_verifying_tls_context():
    # Without context=, starttls() doesn't verify the certificate (MITM can capture secrets).
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    _, kwargs = smtp_instance.starttls.call_args
    context = kwargs.get("context")
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_smtp_starttls_false_skips_it(monkeypatch):
    # Escape hatch for local dev relays without TLS; only an explicit "false" disables it.
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.starttls.assert_not_called()
    smtp_instance.login.assert_called_once()


async def test_login_is_called_when_smtp_user_is_set():
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_called_once_with("invites@example.com", "not-a-real-password")


async def test_login_is_skipped_when_smtp_user_is_unset(monkeypatch):
    # Unauthenticated relays don't advertise AUTH; SMTP_USER presence is the only signal.
    monkeypatch.delenv("SMTP_USER", raising=False)
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_called_once()


async def test_unresolvable_password_ref_fails_loudly_when_smtp_user_is_set(monkeypatch):
    # With SMTP_USER set, a broken password ref must fail, not fall through to an unauthenticated send.
    monkeypatch.setenv("SMTP_PASSWORD_REF", "env:SMTP_PASSWORD_DOES_NOT_EXIST")
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        with pytest.raises(KeyError):
            await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_not_called()


async def test_lifespan_teardown_shuts_down_the_smtp_executor():
    # Unshut executor threads block exit on SIGTERM and leak on reload. Startup deps are mocked
    # so the shared pool isn't touched.
    with (
        patch("services.config.app.db.get_pool", new=AsyncMock()),
        patch("services.config.app.db.close_pool", new=AsyncMock()),
        patch("services.config.app.cache.get_client"),
        patch("services.config.app.cache.close", new=AsyncMock()),
        patch("services.config.app.phone_numbers_service.prewarm", new=AsyncMock(return_value=0)),
        patch("services.config.app.email.close_smtp_executor") as mock_close,
    ):
        async with lifespan(app):
            pass

    mock_close.assert_called_once()


async def test_relay_refusing_starttls_is_a_clean_send_failure_not_a_cleartext_send():
    # No STARTTLS support must fail the send, never fall back to cleartext.
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        smtp_instance.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS extension not supported")
        mock_smtp.return_value.__enter__.return_value = smtp_instance

        with pytest.raises(smtplib.SMTPNotSupportedError):
            await email.send_invite_email(to_email="new-user@example.com", raw_token="tok")

    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_not_called()
