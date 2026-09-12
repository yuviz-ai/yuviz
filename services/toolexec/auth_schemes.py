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

validate_tenant_ref() is the control. It is called both at registration
(services/toolexec/custom_apis.py's _validate_credential_ref, T7) and again
here, at resolution, keyed off the row's own tenant_id — never only at
registration, so a future write path (bulk import, script) cannot bypass it
(lesson 16, lesson 19).

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

import httpx

from libs.config_sdk.secret_resolver import CompositeSecretResolver

TENANT_ENV_PREFIX = "TENANT_"  # env:TENANT_<uuid-hex-upper>_<NAME>
TENANT_SECRET_ROOT = os.environ["TOOLEXEC_TENANT_SECRET_ROOT"]  # NOT the platform k8s mount

_ENV_REF_RE = re.compile(rf"env:{TENANT_ENV_PREFIX}(?P<hex>[0-9A-F]{{32}})_[A-Z0-9_]+")
# No '/' beyond the two fixed separators, so a raw '..' segment cannot even
# reach the regex stage. The .resolve()/relative_to() check below is belt to
# this regex's braces: a symlink planted inside the tenant's own directory
# would otherwise still escape it.
_K8S_REF_RE = re.compile(r"k8s:tenants/(?P<tenant_id>[^/]+)/[A-Za-z0-9._-]+")


def validate_tenant_ref(tenant_id: str, ref: str) -> None:
    """Raises ValueError('credential_ref_outside_tenant_namespace') unless
    `ref` resolves inside a namespace provably owned by `tenant_id`:

        enc:  always allowed — the ciphertext IS the secret, it names
              nothing, so there is no namespace to escape.
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
        anything else (including a literal): rejected.
    """
    # Normalized once: update_custom_api's caller passes the DB row's own
    # tenant_id, an asyncpg.pgproto.pgproto.UUID object, not the str every
    # OTHER caller has (a JSON body field) — uuid.UUID() rejects a UUID
    # instance outright (it expects str/bytes/int), and comparing/joining
    # a Path with the raw object would misbehave the same way below.
    tenant_id = str(tenant_id)

    if ref.startswith("enc:"):
        return

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


# Pointed at TOOLEXEC_TENANT_SECRET_ROOT, never the platform k8s mount —
# this is what makes a validated k8s: ref actually read from the tenant's
# own directory rather than the platform's secret volume.
_tenant_secret_resolver = CompositeSecretResolver(k8s_mount_root=TENANT_SECRET_ROOT)


async def resolve_tenant_ref(tenant_id: str, ref: str) -> str:
    """Re-validates from the row's own tenant_id at resolution time — not
    only at registration — then resolves through the same
    CompositeSecretResolver every other service uses."""
    validate_tenant_ref(tenant_id, ref)
    return await _tenant_secret_resolver.resolve(ref)


# (tenant_id, custom_api_id) -> (access_token, expires_at epoch seconds).
# In-process only, never persisted — a token is a short-lived derived
# artifact, not a credential the tenant authored, so there is nothing here
# for redaction.py to redact and nothing here survives a restart.
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


async def apply(api: dict, headers: dict[str, Any], query_params: dict[str, Any]) -> set[str]:
    """Places api['auth_scheme']'s credential into `headers` or
    `query_params` (mutated in place), resolving every ref through
    resolve_tenant_ref() at call time — never the bare
    CompositeSecretResolver, and never once at registration and reused.
    Raises ValueError("credential_unavailable") — with no ref, no
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
    except Exception as exc:
        raise ValueError("credential_unavailable") from exc
    return set()
