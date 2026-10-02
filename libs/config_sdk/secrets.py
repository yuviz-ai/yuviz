"""Fernet encryption for `enc:` provider credentials stored at rest.

Config Service encrypts before Postgres; consumers decrypt at provider construction.
"""

from __future__ import annotations

import os

ENCRYPTED_PREFIX = "enc:"
_ENV_VAR = "SECRET_ENCRYPTION_KEY"


class SecretEncryptionUnavailable(RuntimeError):
    """Raised rather than falling back to plaintext: a credential store that
    quietly stops encrypting is worse than one that refuses to start."""


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get(_ENV_VAR, "").strip()
    if not key:
        raise SecretEncryptionUnavailable(
            f"{_ENV_VAR} is not set — API keys cannot be encrypted or read back. "
            "deployment/sh/dev.sh generates one; add it to deployment/.env and "
            "restart the config and conversation services."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:  # malformed / wrong length
        raise SecretEncryptionUnavailable(f"{_ENV_VAR} is not a valid Fernet key: {exc}") from None


def generate_key() -> str:
    """A fresh urlsafe-base64 32-byte key, for dev.sh and the docs."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def is_encrypted(ref: str | None) -> bool:
    return bool(ref) and ref.startswith(ENCRYPTED_PREFIX)


def encrypt_secret(plaintext: str) -> str:
    """Returns the `enc:<token>` reference to store in api_key_ref."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty secret")
    return ENCRYPTED_PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ref: str) -> str:
    if not is_encrypted(ref):
        raise ValueError(f"not an encrypted secret reference: {ref[:12]!r}…")
    token = ref[len(ENCRYPTED_PREFIX):].encode()
    try:
        return _fernet().decrypt(token).decode()
    except SecretEncryptionUnavailable:
        raise
    except Exception:
        # Wrong key, or a row from another install. Never echo the ciphertext.
        raise SecretEncryptionUnavailable(
            "stored API key could not be decrypted — SECRET_ENCRYPTION_KEY does not match "
            "the one it was saved with. Re-enter the key on the provider."
        ) from None
