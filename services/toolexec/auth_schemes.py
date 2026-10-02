"""Tenant-namespaced credential refs for custom APIs. Without namespacing, refs like "env:JWT_SECRET" or
"k8s:../../proc/self/environ" would exfiltrate platform secrets. Validated at registration AND every resolution;
errors never include the ref."""

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
# No '/' in the name; the resolve()/relative_to() check also blocks symlink escapes.
_K8S_REF_RE = re.compile(r"k8s:tenants/(?P<tenant_id>[^/]+)/[A-Za-z0-9._-]+")


def validate_tenant_ref(tenant_id: str, ref: str) -> None:
    """Raise unless ref is enc:, env:TENANT_<tenant hex>_<NAME>, or k8s:tenants/<tenant_id>/<name>
    resolving inside that tenant's directory."""
    # Some callers pass an asyncpg UUID object, which uuid.UUID() rejects.
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


# Tenant secret root, never the platform k8s mount.
_tenant_secret_resolver = CompositeSecretResolver(k8s_mount_root=TENANT_SECRET_ROOT)


async def resolve_tenant_ref(tenant_id: str, ref: str) -> str:
    """Re-validate against the row's tenant_id, then resolve."""
    validate_tenant_ref(tenant_id, ref)
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


async def apply(api: dict, headers: dict[str, Any], query_params: dict[str, Any]) -> set[str]:
    """Inject the credential into headers/query_params (resolved at call time); returns injected names.
    Raises ValueError("credential_unavailable") with no secret material; callers must never persist the value."""
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
