from __future__ import annotations

import os

os.environ.setdefault("SECRET_ENCRYPTION_KEY", "test-key-not-a-real-fernet-key")

from libs.config_sdk.secrets import generate_key
from libs.telephony_sdk.providers.cloudonix import CloudonixProvider

os.environ["SECRET_ENCRYPTION_KEY"] = generate_key()

from libs.config_sdk.secrets import encrypt_secret  # noqa: E402


def test_verify_webhook_signature_matches_plaintext_key():
    provider = CloudonixProvider({"domain": "acme.cloudonix.net", "api_keys": ["plain-key-1"]})
    assert provider.verify_webhook_signature("url", {"x-cx-apikey": "plain-key-1"}) is True


def test_verify_webhook_signature_rejects_wrong_key():
    provider = CloudonixProvider({"domain": "acme.cloudonix.net", "api_keys": ["plain-key-1"]})
    assert provider.verify_webhook_signature("url", {"x-cx-apikey": "wrong"}) is False


def test_verify_webhook_signature_rejects_sealed_key_even_if_ciphertext_presented():
    sealed = encrypt_secret("plain-key-1")
    provider = CloudonixProvider({"domain": "acme.cloudonix.net", "api_keys": [sealed]})
    # The presented header can never legitimately be the ciphertext itself,
    # but this asserts the sealed entry is skipped, not compared.
    assert provider.verify_webhook_signature("url", {"x-cx-apikey": sealed}) is False
    assert provider.verify_webhook_signature("url", {"x-cx-apikey": "plain-key-1"}) is False
