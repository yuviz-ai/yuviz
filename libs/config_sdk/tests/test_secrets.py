"""Encrypted provider credentials: round-trip, and refusing rather than degrading."""

from __future__ import annotations

import pytest

from libs.config_sdk.secrets import (
    ENCRYPTED_PREFIX,
    SecretEncryptionUnavailable,
    decrypt_secret,
    encrypt_secret,
    generate_key,
    is_encrypted,
)

KEY = "SECRET_ENCRYPTION_KEY"


@pytest.fixture
def key(monkeypatch):
    k = generate_key()
    monkeypatch.setenv(KEY, k)
    return k


def test_round_trip(key):
    ref = encrypt_secret("AIzaSy-not-a-real-key")
    assert ref.startswith(ENCRYPTED_PREFIX)
    assert decrypt_secret(ref) == "AIzaSy-not-a-real-key"


def test_the_plaintext_never_appears_in_the_stored_reference(key):
    secret = "sk-live-abcdefghijklmnop"
    ref = encrypt_secret(secret)
    # The whole point: a leaked row, backup or log line yields nothing.
    assert secret not in ref


def test_the_same_secret_encrypts_differently_every_time(key):
    # Fernet includes a random IV, so equal ciphertexts can't be used to
    # tell that two providers share a key.
    assert encrypt_secret("same") != encrypt_secret("same")


def test_a_key_from_another_install_is_refused_not_silently_wrong(key, monkeypatch):
    ref = encrypt_secret("secret")
    monkeypatch.setenv(KEY, generate_key())
    with pytest.raises(SecretEncryptionUnavailable) as exc:
        decrypt_secret(ref)
    assert "does not match" in str(exc.value)
    # The error must not carry the ciphertext into a log line.
    assert ref[len(ENCRYPTED_PREFIX):][:20] not in str(exc.value)


def test_a_missing_key_fails_loudly_rather_than_storing_plaintext(monkeypatch):
    monkeypatch.delenv(KEY, raising=False)
    with pytest.raises(SecretEncryptionUnavailable):
        encrypt_secret("secret")
    monkeypatch.setenv(KEY, "")
    with pytest.raises(SecretEncryptionUnavailable):
        encrypt_secret("secret")


def test_a_malformed_key_fails_loudly(monkeypatch):
    monkeypatch.setenv(KEY, "not-a-fernet-key")
    with pytest.raises(SecretEncryptionUnavailable):
        encrypt_secret("secret")


def test_empty_secrets_are_refused(key):
    with pytest.raises(ValueError):
        encrypt_secret("")


def test_is_encrypted_only_claims_its_own_scheme():
    assert is_encrypted("enc:abc")
    for other in ("env:GEMINI_API_KEY", "k8s:ns/s", "AIzaSyRAW", "", None):
        assert not is_encrypted(other)
