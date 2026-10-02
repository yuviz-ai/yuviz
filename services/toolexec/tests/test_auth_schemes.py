"""Tenant credential refs must not name platform or other-tenant secrets.

Rejection cases also assert the underlying resolver is never invoked.
"""

from __future__ import annotations

import os
import uuid

import pytest

from services.toolexec import auth_schemes

TENANT_ID = str(uuid.uuid4())
OTHER_TENANT_ID = str(uuid.uuid4())
TENANT_HEX = uuid.UUID(TENANT_ID).hex.upper()
OTHER_TENANT_HEX = uuid.UUID(OTHER_TENANT_ID).hex.upper()


@pytest.fixture
def resolver_must_not_be_called(monkeypatch):
    """Make any resolver call fail the test loudly."""

    async def _fail(ref: str) -> str:
        raise AssertionError(f"resolver.resolve() was called for a ref that should have been rejected: {ref!r}")

    monkeypatch.setattr(auth_schemes._tenant_secret_resolver, "resolve", _fail)


def _assert_rejected(ref: str, tenant_id: str = TENANT_ID) -> None:
    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        auth_schemes.validate_tenant_ref(tenant_id, ref)


def test_env_platform_jwt_secret_rejected(resolver_must_not_be_called):
    sentinel = "PLATFORM-SENTINEL-JWT-VALUE"
    os.environ["JWT_SECRET"] = sentinel
    _assert_rejected("env:JWT_SECRET")
    assert os.environ["JWT_SECRET"] == sentinel  # untouched, not merely unread


def test_env_platform_encryption_key_rejected(resolver_must_not_be_called):
    sentinel = os.environ["SECRET_ENCRYPTION_KEY"]
    _assert_rejected("env:SECRET_ENCRYPTION_KEY")
    assert os.environ["SECRET_ENCRYPTION_KEY"] == sentinel


def test_env_other_tenants_namespace_rejected(resolver_must_not_be_called):
    sentinel = "OTHER-TENANTS-TOKEN-VALUE"
    os.environ[f"TENANT_{OTHER_TENANT_HEX}_TOKEN"] = sentinel
    _assert_rejected(f"env:TENANT_{OTHER_TENANT_HEX}_TOKEN", tenant_id=TENANT_ID)


def test_k8s_path_traversal_rejected(resolver_must_not_be_called):
    _assert_rejected("k8s:../../etc/passwd")


def test_k8s_other_tenants_namespace_rejected(resolver_must_not_be_called):
    _assert_rejected(f"k8s:tenants/{OTHER_TENANT_ID}/token", tenant_id=TENANT_ID)


def test_k8s_own_tenant_dotdot_leaf_rejected(resolver_must_not_be_called):
    """The regex accepts a '..' leaf; only the path containment check rejects it."""
    _assert_rejected(f"k8s:tenants/{TENANT_ID}/..")


def test_k8s_symlink_escape_rejected(resolver_must_not_be_called, tmp_path):
    """A symlink in the tenant dir pointing outside is caught by the containment check."""
    root = os.environ["TOOLEXEC_TENANT_SECRET_ROOT"]
    tenant_dir = os.path.join(root, "tenants", TENANT_ID)
    os.makedirs(tenant_dir, exist_ok=True)

    outside_secret = tmp_path / "platform-secret"
    outside_secret.write_text("PLATFORM-SENTINEL-FILE-CONTENTS")

    symlink_path = os.path.join(tenant_dir, "evil")
    if os.path.islink(symlink_path) or os.path.exists(symlink_path):
        os.remove(symlink_path)
    os.symlink(outside_secret, symlink_path)

    _assert_rejected(f"k8s:tenants/{TENANT_ID}/evil")
    assert outside_secret.read_text() == "PLATFORM-SENTINEL-FILE-CONTENTS"  # untouched


def test_env_ref_accepted_when_tenant_id_is_asyncpg_uuid_object():
    """An asyncpg UUID tenant_id (from a DB row) behaves like the str form."""
    from asyncpg.pgproto.pgproto import UUID as AsyncpgUUID

    tenant_id_obj = AsyncpgUUID(TENANT_ID)
    auth_schemes.validate_tenant_ref(tenant_id_obj, f"env:TENANT_{TENANT_HEX}_TOKEN")  # must not raise


def test_k8s_ref_accepted_when_tenant_id_is_asyncpg_uuid_object():
    from asyncpg.pgproto.pgproto import UUID as AsyncpgUUID

    root = os.environ["TOOLEXEC_TENANT_SECRET_ROOT"]
    os.makedirs(os.path.join(root, "tenants", TENANT_ID), exist_ok=True)

    tenant_id_obj = AsyncpgUUID(TENANT_ID)
    auth_schemes.validate_tenant_ref(tenant_id_obj, f"k8s:tenants/{TENANT_ID}/token")  # must not raise


@pytest.mark.asyncio
async def test_env_own_namespace_resolves():
    os.environ[f"TENANT_{TENANT_HEX}_TOKEN"] = "own-tenant-value"
    value = await auth_schemes.resolve_tenant_ref(TENANT_ID, f"env:TENANT_{TENANT_HEX}_TOKEN")
    assert value == "own-tenant-value"


@pytest.mark.asyncio
async def test_enc_ref_resolves():
    from libs.config_sdk.secrets import encrypt_secret

    ref = encrypt_secret("my-api-key")
    value = await auth_schemes.resolve_tenant_ref(TENANT_ID, ref)
    assert value == "my-api-key"


@pytest.mark.parametrize(
    "ref,tenant_id",
    [
        ("env:JWT_SECRET", TENANT_ID),
        (f"k8s:tenants/{OTHER_TENANT_ID}/token", TENANT_ID),
    ],
)
@pytest.mark.asyncio
async def test_resolve_tenant_ref_reexamines_namespace_at_resolution(monkeypatch, ref, tenant_id):
    """resolve_tenant_ref() itself re-validates the namespace before resolving."""

    async def _sentinel(_ref: str) -> str:
        return "SENTINEL-SHOULD-NEVER-BE-RETURNED"

    monkeypatch.setattr(auth_schemes._tenant_secret_resolver, "resolve", _sentinel)

    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        await auth_schemes.resolve_tenant_ref(tenant_id, ref)


@pytest.mark.asyncio
async def test_apply_never_puts_the_ref_in_its_own_error_message():
    """apply()'s own error never contains the ref (the executor would mask a leak)."""
    tenant_id = str(uuid.uuid4())
    missing_ref = f"env:TENANT_{uuid.UUID(tenant_id).hex.upper()}_NEVER_SET_TOKEN"
    api = {"auth_scheme": "bearer", "auth_config": {"token_ref": missing_ref},
           "tenant_id": tenant_id, "id": str(uuid.uuid4())}

    with pytest.raises(ValueError) as exc_info:
        await auth_schemes.apply(api, {}, {})

    assert missing_ref not in str(exc_info.value)
    assert str(exc_info.value) == "credential_unavailable"


@pytest.mark.asyncio
async def test_k8s_ref_resolves_a_real_file_planted_in_the_tenant_namespace():
    """A planted tenant secret file resolves end to end to its contents."""
    tenant_id = str(uuid.uuid4())
    root = os.environ["TOOLEXEC_TENANT_SECRET_ROOT"]
    tenant_dir = os.path.join(root, "tenants", tenant_id)
    os.makedirs(tenant_dir, exist_ok=True)
    with open(os.path.join(tenant_dir, "partner_token"), "w") as f:
        f.write("real-tenant-secret-value")

    value = await auth_schemes.resolve_tenant_ref(tenant_id, f"k8s:tenants/{tenant_id}/partner_token")

    assert value == "real-tenant-secret-value"


@pytest.mark.asyncio
async def test_oauth2_token_cached_and_refreshed_60s_before_deployed_expiry(monkeypatch):
    """OAuth2 token is cached, then refetched 60s before expiry (clock mocked, constant untouched)."""
    tenant_id = str(uuid.uuid4())
    tenant_hex = uuid.UUID(tenant_id).hex.upper()
    os.environ[f"TENANT_{tenant_hex}_OAUTH_CID"] = "client-id"
    os.environ[f"TENANT_{tenant_hex}_OAUTH_CSECRET"] = "client-secret"
    config = {
        "token_url": "https://issuer.example.com/token",
        "client_id_ref": f"env:TENANT_{tenant_hex}_OAUTH_CID",
        "client_secret_ref": f"env:TENANT_{tenant_hex}_OAUTH_CSECRET",
    }

    calls = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"access_token": f"tok-{calls['n']}", "expires_in": 100}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, data=None):
            calls["n"] += 1
            return _FakeResponse()

    monkeypatch.setattr(auth_schemes.httpx, "AsyncClient", _FakeAsyncClient)
    auth_schemes._oauth2_token_cache.clear()

    t0 = 1_700_000_000.0
    monkeypatch.setattr(auth_schemes.time, "time", lambda: t0)
    token1 = await auth_schemes._oauth2_client_credentials_token(tenant_id, "api-x", config)
    assert calls["n"] == 1

    # Well inside expiry: served from cache.
    monkeypatch.setattr(auth_schemes.time, "time", lambda: t0 + 10)
    token2 = await auth_schemes._oauth2_client_credentials_token(tenant_id, "api-x", config)
    assert token2 == token1
    assert calls["n"] == 1

    # Exactly expires_at - 60: early refresh kicks in.
    monkeypatch.setattr(auth_schemes.time, "time", lambda: t0 + 100 - 60)
    token3 = await auth_schemes._oauth2_client_credentials_token(tenant_id, "api-x", config)
    assert token3 != token1
    assert calls["n"] == 2
