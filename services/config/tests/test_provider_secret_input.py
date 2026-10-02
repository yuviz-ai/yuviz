"""Provider credential path from request body to column (no database)."""

from __future__ import annotations

import pytest

from libs.config_sdk.secrets import decrypt_secret, generate_key
from services.config.provider_configs import resolve_api_key_input
from services.config.schemas import ProviderConfigCreate, ProviderConfigUpdate


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


def test_create_schema_carries_the_typed_key():
    body = ProviderConfigCreate(name="g", role="llm", engine="gemini", api_key="AIza-typed")
    assert body.api_key == "AIza-typed"


def test_update_schema_carries_the_typed_key():
    # The PATCH router uses exclude_unset; an undeclared field is silently dropped.
    assert ProviderConfigUpdate(api_key="AIza-typed").model_dump(exclude_unset=True) == {
        "api_key": "AIza-typed",
    }


def test_a_typed_key_is_encrypted_not_stored_raw(key):
    stored = resolve_api_key_input("AIza-typed", None)
    assert "AIza-typed" not in stored
    assert decrypt_secret(stored) == "AIza-typed"


def test_pointer_schemes_are_stored_verbatim(key):
    for ref in ("env:GEMINI_API_KEY", "k8s:ns/secret", "enc:abc"):
        assert resolve_api_key_input(None, ref) == ref


def test_a_raw_key_in_the_pointer_field_is_refused(key):
    with pytest.raises(ValueError, match="must point at a secret"):
        resolve_api_key_input(None, "AIzaSyRAW")


def test_no_credential_means_null_not_empty_string(key):
    # "" in a nullable column makes `api_key_ref IS NULL` lie.
    for blank in (None, "", "   "):
        assert resolve_api_key_input(None, blank) is None
