"""Provider credential path from request body to column (no database)."""

from __future__ import annotations

import inspect

import pytest

from libs.config_sdk.secrets import decrypt_secret, generate_key
from services.config.provider_configs import mask_enc, resolve_api_key_input
from services.config.schemas import ProviderConfigCreate, ProviderConfigUpdate
from services.config.tests.call_sites import find_calls


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
    stored = resolve_api_key_input("AIza-typed", None, allow_pointer_schemes=False)
    assert "AIza-typed" not in stored
    assert decrypt_secret(stored) == "AIza-typed"


def test_allow_pointer_schemes_has_no_default():
    param = inspect.signature(resolve_api_key_input).parameters["allow_pointer_schemes"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_pointer_schemes_are_stored_verbatim_for_platform_callers(key):
    for ref in ("env:GEMINI_API_KEY", "k8s:ns/secret"):
        assert resolve_api_key_input(None, ref, allow_pointer_schemes=True) == ref


def test_pointer_and_foreign_enc_refs_fail_with_the_same_text(key):
    messages = set()
    for ref in ("env:GEMINI_API_KEY", "k8s:ns/secret", "enc:abc"):
        with pytest.raises(ValueError) as exc:
            resolve_api_key_input(None, ref, current_ref="enc:other", allow_pointer_schemes=False)
        messages.add(str(exc.value))
    assert messages == {"credential_ref_not_accepted: send the key as api_key"}


def test_enc_ref_is_kept_only_when_it_equals_the_stored_one(key):
    assert resolve_api_key_input(None, "enc:abc", current_ref="enc:abc", allow_pointer_schemes=False) == "enc:abc"
    with pytest.raises(ValueError, match="credential_ref_not_accepted"):
        resolve_api_key_input(None, "enc:abc", current_ref=None, allow_pointer_schemes=True)


def test_stored_sentinel_keeps_the_current_ref(key):
    assert resolve_api_key_input(None, "[stored]", current_ref="enc:abc", allow_pointer_schemes=False) == "enc:abc"
    assert resolve_api_key_input(None, "[stored]", current_ref="env:X", allow_pointer_schemes=False) == "env:X"


def test_stored_sentinel_without_a_current_ref_is_refused(key):
    with pytest.raises(ValueError, match="credential_ref_not_accepted"):
        resolve_api_key_input(None, "[stored]", allow_pointer_schemes=True)


def test_stored_sentinel_as_a_typed_key_is_refused(key):
    with pytest.raises(ValueError, match="credential_ref_not_accepted"):
        resolve_api_key_input("[stored]", None, current_ref="enc:abc", allow_pointer_schemes=True)


def test_a_raw_key_in_the_pointer_field_is_refused(key):
    with pytest.raises(ValueError, match="must point at a secret"):
        resolve_api_key_input(None, "AIzaSyRAW", allow_pointer_schemes=True)


def test_no_credential_means_null_not_empty_string(key):
    # "" in a nullable column makes `api_key_ref IS NULL` lie.
    for blank in (None, "", "   "):
        assert resolve_api_key_input(None, blank, allow_pointer_schemes=False) is None


def test_mask_enc():
    assert mask_enc("enc:abc") == "[stored]"
    assert mask_enc("quarantined") == ""
    assert mask_enc("env:X") == "env:X"
    assert mask_enc(None) is None


# Every caller of the helper and of the service functions that reach it must
# pass the keyword explicitly. The seed script is one of them: it reaches the
# helper through the service functions, which is how it was missed by hand.
_CALLEES = {
    "resolve_api_key_input", "create_provider_config", "update_provider_config",
    "create_tool_provider_config", "update_tool_provider_config",
}
_EXPECTED_CALL_SITES = 53  # +3: test_agent_languages (_cartesia + two provider-edit tests)


def test_every_call_site_passes_allow_pointer_schemes():
    calls = find_calls(_CALLEES, "allow_pointer_schemes")
    missing = [(p, n) for p, n, _, ok in calls if not ok]
    assert missing == [], missing
    assert len(calls) == _EXPECTED_CALL_SITES
    assert any(p == "scripts/seed_default_config.py" for p, _, _, _ in calls)


# --- Tenant scoping on the list route -------------------------------------
# api_key_ref used to be a pointer; an enc: ref carries the sealed credential,
# so an unscoped list is a cross-tenant credential read.
#
# provider_configs.py's local `_require_tenant_access` was replaced by the
# shared `deps.assert_tenant_access` (RLS design T14) — its 403/404 shapes,
# the platform-scoped exemption, and the deliberate narrowing off
# role == "superadmin" onto is_platform_scoped (lesson 24) are covered in
# full by services/config/tests/test_deps_tenant_access.py, not duplicated
# here.
