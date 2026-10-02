"""Custom APIs registry: SSRF endpoint validation and tenant-scoped CRUD.

Endpoint validation runs at registration AND before every outbound call (DNS rebinding).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
from typing import Any
from urllib.parse import urlsplit

from libs.tenancy import platform_conn, tenant_conn

from . import audit, auth_schemes, db, graph

log = logging.getLogger(__name__)

# Absolute deny-list, checked against EVERY resolved A/AAAA record — not
# just the first one a caller happens to control the order of.
_DENIED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
    "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96",
)]

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_allowlist() -> frozenset[str]:
    # Read per call so an env change applies without restart.
    return frozenset(
        h.strip().lower()
        for h in os.environ.get("TOOLEXEC_HTTP_HOST_ALLOWLIST", "").split(",")
        if h.strip()
    )


def _ip_is_denied(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _DENIED_NETS) or not ip.is_global


async def _resolve_addresses(hostname: str, port: int) -> list[str]:
    """Separate function so tests can monkeypatch DNS resolution."""
    try:
        records = await asyncio.get_event_loop().getaddrinfo(
            hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        # Hostname is logged only, never returned: it leaks internal DNS view.
        log.warning("resolve_and_validate_endpoint: DNS resolution failed for %r", hostname)
        raise ValueError("invalid_endpoint_url: dns_resolution_failed") from exc
    return [sockaddr[0] for _family, _type, _proto, _canon, sockaddr in records]


async def resolve_and_validate_endpoint(url: str) -> tuple[str, list[str]]:
    """Return (hostname, allowed_ips) or raise ValueError('invalid_endpoint_url').

    Rejects the whole URL if ANY resolved record is denied; http/non-default ports need the allowlist.
    """
    parts = urlsplit(url)

    if parts.scheme not in ("http", "https"):
        raise ValueError(f"invalid_endpoint_url: scheme must be http or https, got {parts.scheme!r}")

    hostname = parts.hostname
    if not hostname:
        raise ValueError("invalid_endpoint_url: no hostname")
    hostname = hostname.lower()
    allowlisted = hostname in _host_allowlist()

    if parts.scheme == "http" and not allowlisted:
        raise ValueError("invalid_endpoint_url: http requires an allow-listed host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("invalid_endpoint_url: userinfo is not allowed")
    if parts.fragment:
        raise ValueError("invalid_endpoint_url: fragment is not allowed")

    default_port = _DEFAULT_PORTS[parts.scheme]
    if parts.port is not None and parts.port != default_port and not allowlisted:
        raise ValueError("invalid_endpoint_url: non-default port requires an allow-listed host")
    port = parts.port or default_port

    raw_addresses = await _resolve_addresses(hostname, port)
    if not raw_addresses:
        log.warning("resolve_and_validate_endpoint: no addresses resolved for %r", hostname)
        raise ValueError("invalid_endpoint_url: no_addresses_resolved")

    allowed_ips: list[str] = []
    for raw_ip in raw_addresses:
        ip = ipaddress.ip_address(raw_ip.split("%")[0])  # strip an IPv6 zone id, if present
        if _ip_is_denied(ip):
            # Host/IP logged only: returning them would be an internal-network recon channel.
            log.warning(
                "resolve_and_validate_endpoint: %r resolves to a denied address (%s)", hostname, ip,
            )
            raise ValueError("invalid_endpoint_url: resolves to a denied address")
        allowed_ips.append(str(ip))

    return hostname, allowed_ips


class DependentApiExists(Exception):
    """409 at soft-delete: a live custom_api still declares this one as upstream."""


# auth_scheme -> auth_config fields that must be tenant-namespaced refs (enc:/env:/k8s:).
_CREDENTIAL_REF_FIELDS = {
    "api_key": ("key_ref",),
    "bearer": ("token_ref",),
    "oauth2_client_credentials": ("client_id_ref", "client_secret_ref"),
}


def _validate_credential_ref(tenant_id: Any, auth_scheme: str, auth_config: dict) -> None:
    """Reject missing/literal credential fields or refs outside the tenant's namespace."""
    for field in _CREDENTIAL_REF_FIELDS.get(auth_scheme, ()):
        value = auth_config.get(field)
        if not isinstance(value, str) or not value.startswith(("enc:", "env:", "k8s:")):
            raise ValueError(f"credential_ref_not_a_reference: {field}")
        try:
            auth_schemes.validate_tenant_ref(tenant_id, value)
        except ValueError as exc:
            raise ValueError(f"credential_ref_outside_tenant_namespace: {field}") from exc


def _is_well_formed_json_path(path: str) -> bool:
    """Same tiny JSONPath subset graph.extract() consumes: '$.a.b[0].c'."""
    if not path.startswith("$"):
        return False
    rest = path[1:].removeprefix(".")
    if rest == "":
        return True
    return all(graph._SEGMENT_RE.fullmatch(seg) is not None for seg in rest.split("."))


_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")


def _validate_success_template(
    success_template: str | None, sensitive_response_paths: list[str], params: list[dict],
) -> None:
    """Reject placeholders that are malformed, overlap a sensitive path, or name a sensitive param."""
    if not success_template:
        return
    sensitive_param_names = {p["name"] for p in params if p.get("sensitive")}

    for raw in _TEMPLATE_PLACEHOLDER_RE.findall(success_template):
        path = raw.strip()
        if not _is_well_formed_json_path(path):
            raise ValueError(f"invalid_success_template: {raw!r} is not a well-formed JSON path")

        for sensitive_path in sensitive_response_paths:
            if (
                path == sensitive_path
                or path.startswith(sensitive_path + ".")
                or sensitive_path.startswith(path + ".")
            ):
                raise ValueError(
                    f"invalid_success_template: {raw!r} overlaps sensitive_response_paths entry {sensitive_path!r}"
                )

        last_segment = path.rsplit(".", 1)[-1].split("[")[0]
        if last_segment in sensitive_param_names:
            raise ValueError(f"invalid_success_template: {raw!r} names a sensitive param {last_segment!r}")


async def _validate_upstream_params(conn, tenant_id: Any, params: list[dict]) -> None:
    """Upstream ids must be same-tenant and live; the FK can't express either."""
    for param in params:
        if param.get("source") != "upstream":
            continue
        upstream_api_id = param.get("upstream_api_id")
        row = await conn.fetchrow(
            "SELECT id FROM custom_apis WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL",
            upstream_api_id, tenant_id,
        )
        if row is None:
            raise ValueError(f"unknown_upstream_api: {upstream_api_id}")


def _compute_chain_levels(edges: dict[str, list[str]]) -> dict[str, int]:
    """Write-time height per api (leaf=1); kept independent of graph.resolve_order() on purpose.

    Raises ValueError('dependency_cycle' | 'chain_depth_exceeded') before returning anything.
    """
    heights: dict[str, int] = {}

    def _height(node_id: str, ancestor_ids: frozenset[str]) -> int:
        if node_id in heights:
            return heights[node_id]
        if node_id in ancestor_ids:
            raise ValueError("dependency_cycle")
        next_ancestor_ids = ancestor_ids | {node_id}
        upstream_ids = edges.get(node_id, [])
        height = 1 if not upstream_ids else 1 + max(_height(u, next_ancestor_ids) for u in upstream_ids)
        if height > graph.MAX_CHAIN_LEVELS:
            raise ValueError("chain_depth_exceeded")
        heights[node_id] = height
        return height

    for api_id in edges:
        _height(api_id, frozenset())
    return heights


async def _recompute_tenant_chain_levels(conn, tenant_id: Any) -> None:
    """Recompute chain_levels over the whole tenant graph, inside the caller's transaction."""
    api_rows = await conn.fetch(
        "SELECT id FROM custom_apis WHERE tenant_id = $1 AND deleted_at IS NULL", tenant_id,
    )
    edges: dict[str, list[str]] = {str(row["id"]): [] for row in api_rows}

    edge_rows = await conn.fetch(
        "SELECT p.custom_api_id, p.upstream_api_id FROM custom_api_params p "
        "JOIN custom_apis ca ON ca.id = p.custom_api_id AND ca.deleted_at IS NULL "
        "WHERE ca.tenant_id = $1 AND p.source = 'upstream'",
        tenant_id,
    )
    for row in edge_rows:
        edges.setdefault(str(row["custom_api_id"]), []).append(str(row["upstream_api_id"]))

    heights = _compute_chain_levels(edges)

    for api_id, height in heights.items():
        await conn.execute("UPDATE custom_apis SET chain_levels = $2 WHERE id = $1", api_id, height)


async def _replace_params(conn, custom_api_id: Any, params: list[dict]) -> None:
    await conn.execute("DELETE FROM custom_api_params WHERE custom_api_id = $1", custom_api_id)
    for param in params:
        await conn.execute(
            "INSERT INTO custom_api_params "
            "(custom_api_id, name, location, json_type, description, required, source, "
            " literal_value, upstream_api_id, upstream_json_path, sensitive) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)",
            custom_api_id,
            param["name"],
            param["location"],
            param["json_type"],
            param.get("description", ""),
            param.get("required", True),
            param["source"],
            _json_or_none(param.get("literal_value")),
            param.get("upstream_api_id"),
            param.get("upstream_json_path"),
            param.get("sensitive", False),
        )


def _json_or_none(value: Any) -> str | None:
    return json.dumps(value) if value is not None else None


def _decode_custom_api_row(row: Any) -> dict[str, Any]:
    """Decode JSONB columns (asyncpg returns raw strings; no pool codec)."""
    result = dict(row)
    result["auth_config"] = db.json_col(result["auth_config"])
    result["sensitive_response_paths"] = db.json_col(result["sensitive_response_paths"])
    return result


def _decode_literal_value(value: Any) -> Any:
    """Decode once; bare strings are legitimate here, unlike db.json_col's double-encoding guard."""
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError) as exc:
        log.exception("toolexec custom_api_params.literal_value is not decodable JSON")
        raise RuntimeError("toolexec custom_api_params.literal_value is not decodable JSON") from exc


def _decode_param_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["literal_value"] = _decode_literal_value(result["literal_value"])
    return result


def _redact_sensitive_literals(params: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Null sensitive literals in registry reads (viewers can read); the executor sees real values.

    None, not a placeholder: literal params can't be NULL in the DB, so None unambiguously means redacted.
    """
    return [
        {**p, "literal_value": None} if p.get("sensitive") and p.get("literal_value") is not None
        else p
        for p in params
    ]


def _merge_sensitive_literals(new_params: list[dict], old_params_by_name: dict[str, dict]) -> list[dict]:
    """Keep the stored value when a sensitive literal comes back as None (the redacted echo)."""
    merged = []
    for p in new_params:
        if p.get("source") == "literal" and p.get("sensitive") and p.get("literal_value") is None:
            old = old_params_by_name.get(p["name"])
            if old is not None and old.get("source") == "literal" and old.get("literal_value") is not None:
                p = {**p, "literal_value": old["literal_value"]}
        merged.append(p)
    return merged


async def get_custom_api(custom_api_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    """`platform_scoped` must come from `deps.is_platform_scoped`; caller still checks tenant."""
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="custom-apis-admin-by-id") if platform_scoped
        else tenant_conn(pool)
    )
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT * FROM custom_apis WHERE id = $1 AND deleted_at IS NULL", custom_api_id,
        )
        if row is None:
            return None
        result = _decode_custom_api_row(row)
        param_rows = await conn.fetch(
            "SELECT * FROM custom_api_params WHERE custom_api_id = $1 ORDER BY name", custom_api_id,
        )
    result["params"] = _redact_sensitive_literals([_decode_param_row(p) for p in param_rows])
    return result


async def list_custom_apis(tenant_id: Any) -> list[dict[str, Any]]:
    """Return each API with params; the UI edit form relies on them or it wipes edges on save."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM custom_apis WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name", tenant_id,
        )
        apis = [_decode_custom_api_row(row) for row in rows]

        api_ids = [api["id"] for api in apis]
        params_by_api: dict[str, list[dict]] = {}
        if api_ids:
            param_rows = await conn.fetch(
                "SELECT * FROM custom_api_params WHERE custom_api_id = ANY($1::uuid[]) ORDER BY name", api_ids,
            )
            for p in param_rows:
                params_by_api.setdefault(str(p["custom_api_id"]), []).append(_decode_param_row(p))

    for api in apis:
        api["params"] = _redact_sensitive_literals(params_by_api.get(str(api["id"]), []))
    return apis


async def create_custom_api(
    *,
    tenant_id: Any,
    name: str,
    description: str,
    endpoint_url: str,
    method: str,
    body_style: str = "json",
    auth_scheme: str = "none",
    auth_config: dict | None = None,
    side_effecting: bool = True,
    idempotency_header: str | None = None,
    timeout_ms: int | None = None,
    sensitive_response_paths: list[str] | None = None,
    success_template: str | None = None,
    params: list[dict] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    auth_config = auth_config or {}
    sensitive_response_paths = sensitive_response_paths or []
    params = params or []

    _validate_credential_ref(tenant_id, auth_scheme, auth_config)
    await resolve_and_validate_endpoint(endpoint_url)
    _validate_success_template(success_template, sensitive_response_paths, params)

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            # Per-tenant lock: concurrent edits could each pass the depth check and jointly break it.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || $1::text))", str(tenant_id))

            await _validate_upstream_params(conn, tenant_id, params)

            row = await conn.fetchrow(
                "INSERT INTO custom_apis "
                "(tenant_id, name, description, endpoint_url, method, body_style, auth_scheme, "
                " auth_config, side_effecting, idempotency_header, timeout_ms, "
                " sensitive_response_paths, success_template) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12::jsonb, $13) "
                "RETURNING *",
                tenant_id, name, description, endpoint_url, method, body_style, auth_scheme,
                _json_or_none(auth_config) or "{}", side_effecting, idempotency_header, timeout_ms,
                _json_or_none(sensitive_response_paths) or "[]", success_template,
            )
            result = _decode_custom_api_row(row)
            await _replace_params(conn, result["id"], params)

            await _recompute_tenant_chain_levels(conn, tenant_id)

            result = _decode_custom_api_row(
                await conn.fetchrow("SELECT * FROM custom_apis WHERE id = $1", result["id"])
            )
            result["params"] = params

            await audit.write_audit(
                conn, entity_type="custom_api", entity_id=result["id"],
                action="created", user_id=user_id, user_email=user_email, new_value=result,
            )
    return result


_UPDATABLE_FIELDS = {
    "name", "description", "endpoint_url", "method", "body_style", "auth_scheme", "auth_config",
    "side_effecting", "idempotency_header", "timeout_ms", "sensitive_response_paths",
    "success_template",
}


async def update_custom_api(
    custom_api_id: Any,
    *,
    platform_scoped: bool = False,
    params: list[dict] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """`platform_scoped` must come from `deps.is_platform_scoped`.

    Platform writes stamp the row's own tenant so audit_log.tenant_id isn't NULL.
    """
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_custom_api() got non-updatable field(s): {unknown}")

    existing = await get_custom_api(custom_api_id, platform_scoped=platform_scoped)
    if existing is None:
        raise LookupError(f"custom_api {custom_api_id} not found")
    tenant_id = existing["tenant_id"]

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="custom-apis-admin-mutation", stamp_tenant=tenant_id) if platform_scoped
        else tenant_conn(pool)
    )
    async with conn_cm as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM custom_apis WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", custom_api_id,
            )
            if old_row is None:
                raise LookupError(f"custom_api {custom_api_id} not found")
            old = _decode_custom_api_row(old_row)
            tenant_id = old["tenant_id"]

            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || $1::text))", str(tenant_id))

            # Validate the merged final state, not just the patched fields.
            final_auth_scheme = fields.get("auth_scheme", old["auth_scheme"])
            final_auth_config = fields.get("auth_config", old["auth_config"])
            final_endpoint_url = fields.get("endpoint_url", old["endpoint_url"])
            final_success_template = fields.get("success_template", old["success_template"])
            final_sensitive_paths = fields.get("sensitive_response_paths", old["sensitive_response_paths"])

            old_params = [
                _decode_param_row(p) for p in await conn.fetch(
                    "SELECT * FROM custom_api_params WHERE custom_api_id = $1", custom_api_id,
                )
            ]

            if params is not None:
                # Merge before validation/writes so they see the preserved secret.
                params = _merge_sensitive_literals(params, {p["name"]: p for p in old_params})
                final_params = params
            else:
                final_params = old_params

            _validate_credential_ref(tenant_id, final_auth_scheme, final_auth_config)
            await resolve_and_validate_endpoint(final_endpoint_url)
            _validate_success_template(final_success_template, final_sensitive_paths, final_params)

            if params is not None:
                await _validate_upstream_params(conn, tenant_id, params)

            if fields:
                columns = list(fields.keys())
                set_clause = ", ".join(
                    f"{col} = ${i + 2}::jsonb" if col in ("auth_config", "sensitive_response_paths")
                    else f"{col} = ${i + 2}"
                    for i, col in enumerate(columns)
                )
                values = [
                    _json_or_none(fields[col]) if col in ("auth_config", "sensitive_response_paths")
                    else fields[col]
                    for col in columns
                ]
                await conn.execute(
                    f"UPDATE custom_apis SET {set_clause}, updated_at = now() WHERE id = $1",
                    custom_api_id, *values,
                )

            if params is not None:
                await _replace_params(conn, custom_api_id, params)

            await _recompute_tenant_chain_levels(conn, tenant_id)

            new_row = await conn.fetchrow("SELECT * FROM custom_apis WHERE id = $1", custom_api_id)
            new = _decode_custom_api_row(new_row)
            new["params"] = [
                _decode_param_row(p) for p in await conn.fetch(
                    "SELECT * FROM custom_api_params WHERE custom_api_id = $1", custom_api_id,
                )
            ]

            await audit.write_audit(
                conn, entity_type="custom_api", entity_id=custom_api_id, action="updated",
                user_id=user_id, user_email=user_email, old_value=old, new_value=new,
            )
    return new


async def soft_delete_custom_api(
    custom_api_id: Any, *, platform_scoped: bool = False,
    user_id: Any | None = None, user_email: str | None = None,
) -> None:
    """See update_custom_api's docstring for `platform_scoped`/`stamp_tenant`."""
    existing = await get_custom_api(custom_api_id, platform_scoped=platform_scoped)
    if existing is None:
        raise LookupError(f"custom_api {custom_api_id} not found")

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="custom-apis-admin-mutation", stamp_tenant=existing["tenant_id"])
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM custom_apis WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", custom_api_id,
            )
            if old_row is None:
                raise LookupError(f"custom_api {custom_api_id} not found")
            old = dict(old_row)

            dependent = await conn.fetchrow(
                "SELECT ca.id, ca.name FROM custom_api_params p "
                "JOIN custom_apis ca ON ca.id = p.custom_api_id AND ca.deleted_at IS NULL "
                "WHERE p.upstream_api_id = $1",
                custom_api_id,
            )
            if dependent is not None:
                raise DependentApiExists(
                    f"custom_api {custom_api_id} is still declared as an upstream dependency "
                    f"by {dependent['name']!r} ({dependent['id']})"
                )

            await conn.execute("UPDATE custom_apis SET deleted_at = now() WHERE id = $1", custom_api_id)

            # No cascade to agent_custom_apis: readers join on deleted_at IS NULL.

            await audit.write_audit(
                conn, entity_type="custom_api", entity_id=custom_api_id, action="deleted",
                user_id=user_id, user_email=user_email, old_value=old,
            )
