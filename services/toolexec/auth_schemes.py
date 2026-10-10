"""
Tenant-namespaced credential ref validation and resolution — the boundary
between a tenant-authored custom_apis.auth_config value and the platform's
shared CompositeSecretResolver (libs/config_sdk/secret_resolver.py).

A tenant admin authors these refs, so scheme-shape validation alone is not
enough: EnvResolver returns ANY process env var
(libs/config_sdk/secret_resolver.py) and K8sFileResolver does an unguarded
Path(mount_root) / ref join, so a ref like "env:JWT_SECRET" or
"k8s:../../proc/self/environ" would otherwise exfiltrate the platform's
signing key / encryption key / arbitrary files to a tenant-chosen
endpoint_url (finding 1).

validate_tenant_ref() is the write-side control, called at registration
(services/toolexec/custom_apis.py's _validate_credential_ref). It accepts
only a tenant-bound `enc:t1.` ref that opens for the row's own tenant:
a legacy `enc:` Fernet token is a bearer capability any tenant could paste
into its own row, and `env:`/`k8s:` are platform-operator input a tenant
must not author. resolve_tenant_ref() is the read-side control, keyed off
the row's own tenant_id at call time — never only at registration, so a
future write path (bulk import, script) cannot bypass it (lesson 16,
lesson 19). It still reads `env:`/`k8s:` rows that predate the write-side
refusal, inside the namespace check, until the quarantine script retires them.

apply() (T15) is the runtime half: it places a resolved credential into an
outbound step's headers/query, calling resolve_tenant_ref() — never the
bare CompositeSecretResolver — at CALL TIME, every time, for every scheme.
An unresolvable, absent, or out-of-namespace ref raises ValueError
("credential_unavailable") before any request is built, with the ref
itself never in the message (AC 12): the caller (executor.py) must not put
it in a step row or a log line either.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from libs.config_sdk.secret_resolver import CompositeSecretResolver
from libs.config_sdk.secrets import decrypt_tenant_secret, is_tenant_bound

TENANT_ENV_PREFIX = "TENANT_"  # env:TENANT_<uuid-hex-upper>_<NAME>
TENANT_SECRET_ROOT = os.environ["TOOLEXEC_TENANT_SECRET_ROOT"]  # NOT the platform k8s mount

_ENV_REF_RE = re.compile(rf"env:{TENANT_ENV_PREFIX}(?P<hex>[0-9A-F]{{32}})_[A-Z0-9_]+")
# No '/' in the name; the resolve()/relative_to() check also blocks symlink escapes.
_K8S_REF_RE = re.compile(r"k8s:tenants/(?P<tenant_id>[^/]+)/[A-Za-z0-9._-]+")


class ReconnectRequired(ValueError):
    """The tenant's OAuth connection cannot supply a token until an admin
    reconnects it."""


def validate_tenant_ref(tenant_id: str, ref: str) -> None:
    """Write-side check. Raises ValueError('credential_ref_outside_tenant_namespace')
    unless `ref` is an `enc:t1.` ciphertext that opens for `tenant_id`.

        enc:t1.  allowed only if decrypt_tenant_secret(tenant_id, ref)
                 succeeds (the result is discarded). Another tenant's
                 ciphertext fails the AEAD tag.
        enc:     (legacy Fernet) rejected: it names no tenant, so it opens
                 for whoever pastes it.
        env:/k8s: rejected outright: a tenant admin may not author a
                 pointer into platform-operator namespaces.
        anything else (including a literal): rejected.
    """
    if is_tenant_bound(ref):
        try:
            decrypt_tenant_secret(tenant_id, ref)
        except ValueError:
            raise ValueError("credential_ref_outside_tenant_namespace") from None
        return
    raise ValueError("credential_ref_outside_tenant_namespace")


def _validate_pointer_ref(tenant_id: str, ref: str) -> None:
    """Read-side namespace check for a pre-existing `env:`/`k8s:` row:

        env:  must fullmatch env:TENANT_<hex>_<NAME> where
              hex = uuid.UUID(tenant_id).hex.upper(). No platform variable
              (JWT_SECRET, SECRET_ENCRYPTION_KEY, POSTGRES_DSN, ...) can
              match, and one tenant cannot name another's because the hex
              is fixed by the row's own tenant_id.
        k8s:  must fullmatch k8s:tenants/<tenant_id>/<name> with <name>
              containing no '/' — AND
              (Path(TENANT_SECRET_ROOT) / rel).resolve() must be
              relative_to (Path(TENANT_SECRET_ROOT).resolve() / 'tenants'
              / tenant_id), so a symlink inside the tenant's own directory
              cannot point outside it either.
        anything else: rejected.
    """
    # Normalized once: update_custom_api's caller passes the DB row's own
    # tenant_id, an asyncpg.pgproto.pgproto.UUID object, not the str every
    # OTHER caller has (a JSON body field) — uuid.UUID() rejects a UUID
    # instance outright (it expects str/bytes/int), and comparing/joining
    # a Path with the raw object would misbehave the same way below.
    tenant_id = str(tenant_id)

    if ref.startswith("env:"):
        expected_hex = uuid.UUID(tenant_id).hex.upper()
        match = _ENV_REF_RE.fullmatch(ref)
        if match is None or match.group("hex") != expected_hex:
            raise ValueError("credential_ref_outside_tenant_namespace")
        return

    if ref.startswith("k8s:"):
        match = _K8S_REF_RE.fullmatch(ref)
        if match is None or match.group("tenant_id") != tenant_id:
            raise ValueError("credential_ref_outside_tenant_namespace")
        root = Path(TENANT_SECRET_ROOT).resolve()
        allowed_dir = root / "tenants" / tenant_id
        target = (root / ref.removeprefix("k8s:")).resolve()
        try:
            target.relative_to(allowed_dir)
        except ValueError:
            raise ValueError("credential_ref_outside_tenant_namespace") from None
        return

    raise ValueError("credential_ref_outside_tenant_namespace")


# Tenant secret root, never the platform k8s mount.
_tenant_secret_resolver = CompositeSecretResolver(k8s_mount_root=TENANT_SECRET_ROOT)


async def resolve_tenant_ref(tenant_id: str, ref: str) -> str:
    """Re-validates from the row's own tenant_id at resolution time — not
    only at registration. `enc:t1.` opens only through
    decrypt_tenant_secret for this tenant; any other `enc:` (legacy Fernet)
    is refused here, never decrypted. `env:`/`k8s:` rows that predate the
    write-side refusal resolve through the namespace check and the
    tenant-rooted resolver."""
    if ref.startswith("enc:"):
        if not is_tenant_bound(ref):
            raise ValueError("credential_ref_outside_tenant_namespace")
        return decrypt_tenant_secret(tenant_id, ref)
    _validate_pointer_ref(tenant_id, ref)
    return await _tenant_secret_resolver.resolve(ref)


# (tenant_id, custom_api_id) -> (access_token, expires_at epoch seconds); in-process only.
_oauth2_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


async def _oauth2_client_credentials_token(tenant_id: str, custom_api_id: str, config: dict) -> str:
    cache_key = (str(tenant_id), str(custom_api_id))
    cached = _oauth2_token_cache.get(cache_key)
    now = time.time()
    if cached is not None and now < cached[1] - 60:  # refresh 60s before expiry
        return cached[0]

    client_id = await resolve_tenant_ref(tenant_id, config["client_id_ref"])
    client_secret = await resolve_tenant_ref(tenant_id, config["client_secret_ref"])
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            config["token_url"],
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": config.get("scope", ""),
            },
        )
        resp.raise_for_status()
        body = resp.json()

    token = body["access_token"]
    expires_in = body.get("expires_in", 3600)
    _oauth2_token_cache[cache_key] = (token, now + expires_in)
    return token


async def apply(api: dict, headers: dict[str, Any], query_params: dict[str, Any], *, effective_url: str) -> set[str]:
    """Places api['auth_scheme']'s credential into `headers` or
    `query_params` (mutated in place), resolving every ref through
    resolve_tenant_ref() at call time — never the bare
    CompositeSecretResolver, and never once at registration and reused.
    Raises ReconnectRequired when an OAuth connection needs an admin to
    reconnect it. Otherwise raises ValueError("credential_unavailable") — with no ref, no
    auth_config value, and no partial credential in the message — if the
    ref cannot be resolved (out-of-namespace, missing platform/tenant
    secret, decrypt failure, or an unreachable OAuth2 token endpoint).

    Returns the set of header/query-param NAMES it just injected — never
    the values — so the caller (executor.py) can exclude the resolved
    credential from whatever it persists or hashes. The caller must not
    put the resolved value itself in a step row, a log line, or the
    arguments hash either."""
    scheme = api["auth_scheme"]
    config = api["auth_config"] or {}
    tenant_id = api["tenant_id"]
    custom_api_id = api["id"]

    try:
        if scheme == "none":
            return set()
        if scheme == "api_key":
            value = await resolve_tenant_ref(tenant_id, config["key_ref"])
            if config.get("location") == "query":
                query_params[config["name"]] = value
            else:
                headers[config["name"]] = value
            return {config["name"]}
        elif scheme == "bearer":
            value = await resolve_tenant_ref(tenant_id, config["token_ref"])
            headers["Authorization"] = f"Bearer {value}"
            return {"Authorization"}
        elif scheme == "oauth2_client_credentials":
            token = await _oauth2_client_credentials_token(tenant_id, custom_api_id, config)
            headers["Authorization"] = f"Bearer {token}"
            return {"Authorization"}
        elif scheme == "oauth2_authorization_code":
            from . import oauth  # function-local: oauth imports this module

            token, provider, api_base_url, _auth_kind = await oauth.access_token_for(
                tenant_id, api["oauth_connection_id"],
            )
            # A connector token goes only to the host this row is bound to, judged on the URL actually dialed.
            if not oauth.provider_host_allowed(
                provider, urlsplit(effective_url).hostname, api_base_url, base_source=api["endpoint_base_source"],
            ):
                raise ValueError("credential_unavailable")
            header_name, header_value = provider.auth_header
            headers[header_name] = header_value.format(token=token)
            return {header_name}
    except ReconnectRequired:
        raise
    except Exception as exc:
        raise ValueError("credential_unavailable") from exc
    return set()
