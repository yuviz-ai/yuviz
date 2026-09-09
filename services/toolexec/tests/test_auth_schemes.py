"""
Namespace-containment tests for services/toolexec/auth_schemes.py (T4,
finding 1 — a tenant-authored credential ref must not be able to name a
platform secret). Every rejection case asserts BOTH the error string and
that the underlying resolver was never invoked: `_tenant_secret_resolver`
is monkeypatched to fail the test outright if `resolve()` is ever called,
so a regression that lets a bad ref through fails loudly here rather than
merely returning the wrong value.
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
    """Every rejection test uses this so a validate_tenant_ref bug that lets
    a bad ref through is caught here — as a loud AssertionError from the
    resolver itself — rather than by a merely-wrong return value."""

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
    """_K8S_REF_RE's leaf class `[A-Za-z0-9._-]+` matches '..' literally (own
    tenant id, no '/' in the leaf), so the regex alone would ACCEPT
    'k8s:tenants/<own-tid>/..' — the only thing rejecting it is the
    .resolve()/relative_to() containment check the module comment calls
    'belt' to the regex. This is the exact case finding 3 named as untested.

    Verified by mutation: replacing the `target.relative_to(allowed_dir)`
    containment check with an unconditional `return` (i.e. deleting the
    'belt') makes this ref resolve to the tenant's own parent directory
    instead of raising — confirmed locally, then the check was restored.
    """
    _assert_rejected(f"k8s:tenants/{TENANT_ID}/..")


def test_k8s_symlink_escape_rejected(resolver_must_not_be_called, tmp_path):
    """The regex alone accepts this ref (own tenant, no '/' in the leaf
    segment) — only the .resolve()/relative_to() containment check catches
    a symlink planted inside the tenant's own directory that points
    outside it. This is the case that fails if only the regex is
    implemented (per the design's test plan note)."""
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
    """Defect 4: update_custom_api passes tenant_id straight off the DB row
    — an asyncpg.pgproto.pgproto.UUID object, not the str every other
    caller has (a JSON body field). uuid.UUID() rejects a UUID instance
    outright ('object has no attribute replace'), so PATCH always 500'd
    for an env:/k8s: ref. Must behave identically to the str form."""
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
    """Drives resolve_tenant_ref() itself (not validate_tenant_ref directly),
    with the underlying resolver monkeypatched to return a sentinel rather
    than raise/fail-loud. This is the case finding 1 identified as missing:
    every other rejection test in this file calls validate_tenant_ref
    directly, so deleting the `validate_tenant_ref(tenant_id, ref)` line
    inside resolve_tenant_ref (auth_schemes.py:96) left all prior tests
    green. Here, that deletion makes the sentinel come back as the
    'resolved' value instead of the call raising — so this test fails
    under that mutation.

    Verified by mutation: commenting out the `validate_tenant_ref(tenant_id,
    ref)` call in `resolve_tenant_ref` turns this from a raised ValueError
    into a return of "SENTINEL-SHOULD-NEVER-BE-RETURNED" — confirmed
    locally, then the line was restored.
    """

    async def _sentinel(_ref: str) -> str:
        return "SENTINEL-SHOULD-NEVER-BE-RETURNED"

    monkeypatch.setattr(auth_schemes._tenant_secret_resolver, "resolve", _sentinel)

    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        await auth_schemes.resolve_tenant_ref(tenant_id, ref)


@pytest.mark.asyncio
async def test_apply_never_puts_the_ref_in_its_own_error_message():
    """Unit-level proof of apply()'s own docstring claim, independent of
    whatever executor.py does with the exception it catches (executor.py
    discards apply()'s message entirely and substitutes a fixed
    'credential_unavailable' string, which would mask a leak here too —
    so this has to be checked directly against apply(), not only
    end-to-end)."""
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
    """QA 'Not covered': only the k8s: rejection paths were driven; no
    secret file was ever planted, so a successful tenant-namespaced read
    was unverified. Plants a real file under TOOLEXEC_TENANT_SECRET_ROOT
    and drives resolve_tenant_ref end to end (validate + the real
    K8sFileResolver), asserting the file's actual contents come back —
    fails if the resolver is pointed at the wrong root or the ref
    resolution logic is broken, not merely if validation is."""
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
    """Design test plan: 'OAuth2 fetches once, reuses within expiry,
    re-fetches after' plus the 60s early-refresh margin
    (auth_schemes.py's `now < cached[1] - 60`) — neither was exercised by
    any test (QA 'Not covered': no OAuth2 token endpoint was available).
    Fakes only the token endpoint transport (httpx.AsyncClient), driving
    the real cache dict and the real time-comparison logic; the clock is
    advanced by monkeypatching auth_schemes.time.time, never by lowering
    the 60s constant (lesson 25 — the constant IS the thing under test).

    Mutation proof: removing the `- 60` margin (i.e. only refreshing once
    now >= cached[1]) makes the third call at exactly expires_at-60 reuse
    the cached token and calls['n'] stays at 1 — this assertion fails."""
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

    # Still well inside expiry, and inside the fetch's own 60s margin from
    # now — must be served from cache with NO new call.
    monkeypatch.setattr(auth_schemes.time, "time", lambda: t0 + 10)
    token2 = await auth_schemes._oauth2_client_credentials_token(tenant_id, "api-x", config)
    assert token2 == token1
    assert calls["n"] == 1

    # Exactly expires_at - 60: the deployed early-refresh margin must
    # already have kicked in, even though the token has not technically
    # expired yet.
    monkeypatch.setattr(auth_schemes.time, "time", lambda: t0 + 100 - 60)
    token3 = await auth_schemes._oauth2_client_credentials_token(tenant_id, "api-x", config)
    assert token3 != token1
    assert calls["n"] == 2
