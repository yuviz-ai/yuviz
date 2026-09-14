"""
services/toolexec/custom_apis.py — the custom_apis registry module.

resolve_and_validate_endpoint() (T5) is the SSRF guard against a
tenant-registered endpoint_url: it runs at registration (create/update,
below) AND again before every outbound call (executor.py step 4, T12 —
not yet implemented), since a registration-time-only check loses to DNS
rebinding — the same hostname can resolve to a different, private address
by the time the call actually fires.

create_custom_api / update_custom_api / soft_delete_custom_api (T7) are
the registry CRUD: same-tenant validation of every upstream_api_id,
tenant-namespaced credential ref validation (auth_schemes.py, T4),
endpoint validation (above), success_template placeholder validation, and
chain_levels/cycle recompute across the whole tenant graph inside a
per-tenant advisory-locked transaction — chain_levels is a WRITE-TIME
denormalization computed independently here, not by calling
graph.resolve_order() (that function is the READ-TIME backstop the
executor calls; the two must not share a bug, so they are separate code).
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
    # Read fresh each call rather than cached at import time: an operator
    # env-var change should take effect without a service restart forcing
    # a redeploy race with this specific knob.
    return frozenset(
        h.strip().lower()
        for h in os.environ.get("TOOLEXEC_HTTP_HOST_ALLOWLIST", "").split(",")
        if h.strip()
    )


def _ip_is_denied(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _DENIED_NETS) or not ip.is_global


async def _resolve_addresses(hostname: str, port: int) -> list[str]:
    """Isolated so tests can monkeypatch DNS resolution directly instead of
    reaching into asyncio/socket internals (also where a multi-record host
    with one private address is exercised)."""
    try:
        records = await asyncio.get_event_loop().getaddrinfo(
            hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        # The hostname stays OUT of the client-facing message (finding 2):
        # "DNS resolution failed for 'internal-billing.corp'" tells a
        # tenant_admin the platform could not resolve a name they chose,
        # which is itself a fact about the platform's internal DNS view —
        # confirmed only in the server log.
        log.warning("resolve_and_validate_endpoint: DNS resolution failed for %r", hostname)
        raise ValueError("invalid_endpoint_url: dns_resolution_failed") from exc
    return [sockaddr[0] for _family, _type, _proto, _canon, sockaddr in records]


async def resolve_and_validate_endpoint(url: str) -> tuple[str, list[str]]:
    """Returns (hostname, allowed_ips) or raises ValueError('invalid_endpoint_url').

    - scheme must be 'https', unless the hostname is in the operator-configured
      TOOLEXEC_HTTP_HOST_ALLOWLIST (an explicit host list, never "any private
      address").
    - no userinfo, no fragment, no non-default port unless the host is
      allow-listed.
    - getaddrinfo() the host for BOTH families and check EVERY returned
      record through ipaddress.ip_address() — which normalizes decimal/octal/
      hex IPv4 and IPv4-mapped IPv6 ('::ffff:127.0.0.1') to their real value.
      If ANY record falls in _DENIED_NETS (or is not .is_global), reject the
      WHOLE url — never dial "the good one".
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
            # Neither the hostname nor the resolved address reaches the
            # client (finding 2): "resolves to a denied address (10.0.3.7)"
            # both confirms the host exists AND discloses its internal
            # address — an internal-network recon channel from a
            # tenant-scoped UI. Logged server-side only.
            log.warning(
                "resolve_and_validate_endpoint: %r resolves to a denied address (%s)", hostname, ip,
            )
            raise ValueError("invalid_endpoint_url: resolves to a denied address")
        allowed_ips.append(str(ip))

    return hostname, allowed_ips


class DependentApiExists(Exception):
    """409 at soft-delete — a live (non-soft-deleted) custom_apis row still
    declares this one as an upstream dependency. Distinct from ValueError
    (400) and LookupError (404): this is a real conflict with other
    tenant-owned data, not a bad request or a missing id."""


# auth_scheme -> the auth_config field name(s) that must be a tenant-
# namespaced ref (enc:/env:/k8s:), per custom_apis.auth_config's shape
# (see database/schema.sql's custom_apis comment). 'none' needs nothing.
_CREDENTIAL_REF_FIELDS = {
    "api_key": ("key_ref",),
    "bearer": ("token_ref",),
    "oauth2_client_credentials": ("client_id_ref", "client_secret_ref"),
}


def _validate_credential_ref(tenant_id: Any, auth_scheme: str, auth_config: dict) -> None:
    """Raises ValueError('credential_ref_not_a_reference: <field>') if a
    required field is missing or a literal secret (no enc:/env:/k8s:
    scheme), or ValueError('credential_ref_outside_tenant_namespace:
    <field>') if auth_schemes.validate_tenant_ref() rejects it — the same
    control T4 built, called again here at registration (and again by
    resolve_tenant_ref at call time, T15 — never only here)."""
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
    """Parses every {{$.path}} placeholder in success_template and rejects
    the save (ValueError('invalid_success_template: <placeholder>')) unless
    the path is well-formed AND is neither equal to, nor a descendant or
    ancestor of, any entry in sensitive_response_paths, AND its last
    segment does not name a custom_api_params row marked sensitive.
    Re-run on every update whose sensitive_response_paths or params
    change — not only at creation — so a path made sensitive after the
    template already references it is caught (the PATCH-after-the-fact
    case)."""
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
    """Same-tenant validation of every 'upstream'-sourced param's
    upstream_api_id (AC 17) — the FK alone cannot express "same tenant",
    and it also cannot express "not soft-deleted"."""
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
    """Post-order height computation over {api_id: [upstream_api_id, ...]}
    — a WRITE-TIME recompute, independently implemented from
    graph.resolve_order() (that one is the READ-TIME backstop the executor
    calls; the two must not share a bug). height[api] = 1 for a leaf, else
    1 + max(height[upstream]). Unlike resolve_order()'s runtime depth check
    (which is path-dependent — the same api can be reached at different
    ancestor depths from different targets), an api's OWN height is a pure
    property of the subgraph beneath it, so memoizing it is safe here: it
    never varies with how it is reached, only with what depends on it.

    Raises ValueError('dependency_cycle') or
    ValueError('chain_depth_exceeded') (against graph.MAX_CHAIN_LEVELS)
    before returning anything, so the caller's transaction can roll back
    the whole write with no partial chain_levels update ever committed.
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
    """Recomputes chain_levels for the edited row AND every transitive
    dependent (AC 11) — the whole tenant's graph, read back from the rows
    this same transaction just wrote, so the recompute sees its own writes
    before anything commits. Cheap: registry CRUD is admin-facing, not the
    hot path, and a tenant's own API count is small."""
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
    """asyncpg returns JSONB columns as raw strings (no pool codec — see
    db.json_col) — decode the two this table has so every reader gets a
    Python dict/list back, the same shape it was written with."""
    result = dict(row)
    result["auth_config"] = db.json_col(result["auth_config"])
    result["sensitive_response_paths"] = db.json_col(result["sensitive_response_paths"])
    return result


def _decode_literal_value(value: Any) -> Any:
    """literal_value is a JSONB column whose payload IS an arbitrary JSON
    scalar/object/array by design — including an ordinary string like
    "ACC-42" — so db.json_col's blanket 'a decoded string means
    double-encoding' guard (correct for auth_config/sensitive_response_paths,
    which are never legitimately bare scalars) is the wrong check here and
    would 500 a perfectly normal string literal. Decode once and accept
    whatever comes back; only genuinely undecodable JSON is an error."""
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
    """Registry reads (list/get) are behind bare Depends(get_current_user)
    — every authenticated role in the tenant, including viewer — and
    unlike auth_config, nothing validates a literal param's value against
    being a real secret (_validate_credential_ref only constrains
    auth_scheme fields). A param the admin themselves marked `sensitive`
    (the exact shape the UI's checkbox invites, e.g. a bearer token typed
    into a header field) must not come back as plaintext here, whatever
    its source. Only touches the REGISTRY view — executor.py's own
    _decode_param_row calls (never through this function) still see the
    real value, which is what actually places it on the outbound call.

    Redacted to None, deliberately NOT a placeholder string like
    "[redacted]": a source='literal' param is DB-constrained to never
    legitimately hold NULL (custom_api_params_source_shape), so None here
    is unambiguous — it can only mean "not shown", never a real value —
    and update_custom_api's merge (below) can tell "the caller echoed
    back what they were shown" from "the caller supplied a genuine new
    value" without comparing against redacted TEXT, which would silently
    misfire the day a tenant's real secret IS that exact string."""
    return [
        {**p, "literal_value": None} if p.get("sensitive") and p.get("literal_value") is not None
        else p
        for p in params
    ]


def _merge_sensitive_literals(new_params: list[dict], old_params_by_name: dict[str, dict]) -> list[dict]:
    """update_custom_api's write-side half of lesson 33: an edit form's
    only source for a sensitive literal is the redacted (None) value
    _redact_sensitive_literals hands back, so a save that echoes it
    unchanged has no legitimate way to supply the real one. None here
    means "not provided" — the same absence-preserves convention
    update_custom_api already applies to the top-level `params` list
    itself — never "clear it": preserve the STORED value instead of
    writing the caller's None over it. A genuinely non-null incoming
    literal_value (a deliberate change, including to a new sensitive
    value) always passes through untouched."""
    merged = []
    for p in new_params:
        if p.get("source") == "literal" and p.get("sensitive") and p.get("literal_value") is None:
            old = old_params_by_name.get(p["name"])
            if old is not None and old.get("source") == "literal" and old.get("literal_value") is not None:
                p = {**p, "literal_value": old["literal_value"]}
        merged.append(p)
    return merged


async def get_custom_api(custom_api_id: Any) -> dict[str, Any] | None:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM custom_apis WHERE id = $1 AND deleted_at IS NULL", custom_api_id,
    )
    if row is None:
        return None
    result = _decode_custom_api_row(row)
    param_rows = await pool.fetch(
        "SELECT * FROM custom_api_params WHERE custom_api_id = $1 ORDER BY name", custom_api_id,
    )
    result["params"] = _redact_sensitive_literals([_decode_param_row(p) for p in param_rows])
    return result


async def list_custom_apis(tenant_id: Any) -> list[dict[str, Any]]:
    """Returns each API WITH its params (lesson 33): this is the admin-ui
    panel's only source for the Edit form, and update_custom_api only
    replaces params when the caller explicitly sends the key (`params is
    not None`) — so a list response missing `params` is what makes the
    form fall back to an empty array and unknowingly wipe a real API's
    dependency edges on save, rather than the write layer itself treating
    absent as "clear it"."""
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT * FROM custom_apis WHERE tenant_id = $1 AND deleted_at IS NULL ORDER BY name", tenant_id,
    )
    apis = [_decode_custom_api_row(row) for row in rows]

    api_ids = [api["id"] for api in apis]
    params_by_api: dict[str, list[dict]] = {}
    if api_ids:
        param_rows = await pool.fetch(
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
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serializes every create/update for this tenant so two
            # concurrent edits cannot each individually pass the depth
            # check and jointly break it (lesson 8: check-then-act on a
            # shared row is a race — this makes the check-and-recompute
            # one atomic section per tenant, not two independent reads).
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

            # Recomputes chain_levels for this row and every transitive
            # dependent (there are none yet for a brand-new row, but this
            # keeps create/update on one code path) — raises before commit
            # if the ceiling or a cycle would be violated (AC 11).
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
    params: list[dict] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_custom_api() got non-updatable field(s): {unknown}")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM custom_apis WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", custom_api_id,
            )
            if old_row is None:
                raise LookupError(f"custom_api {custom_api_id} not found")
            old = _decode_custom_api_row(old_row)
            tenant_id = old["tenant_id"]

            # Serialized per tenant — see create_custom_api's comment (lesson 8).
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || $1::text))", str(tenant_id))

            # The FINAL merged state is what every validation below checks —
            # not just the fields this call happens to touch — so PATCHing
            # sensitive_response_paths alone re-validates an unrelated,
            # already-stored success_template against it (the
            # PATCH-after-the-fact case).
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
                # Merge BEFORE any validation/write below sees `params` —
                # every subsequent use (success_template check, upstream
                # validation, the actual _replace_params write) must see
                # the real preserved secret, not the caller's None.
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
    custom_api_id: Any, *, user_id: Any | None = None, user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
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

            # Deliberately NOT cascading to agent_custom_apis.enabled = false:
            # T11's ownership query joins agent_custom_apis.custom_api_id =
            # custom_apis.id against a custom_apis row already filtered on
            # deleted_at IS NULL, so once this row is soft-deleted, no
            # agent_custom_apis row referencing it can join regardless of
            # its stale `enabled` flag — the JOIN, not a cascade write, is
            # what makes it unexecutable the very next turn (lesson 16).
            # A cascade UPDATE here would be redundant work maintaining an
            # invariant the read side already guarantees by construction.

            await audit.write_audit(
                conn, entity_type="custom_api", entity_id=custom_api_id, action="deleted",
                user_id=user_id, user_email=user_email, old_value=old,
            )
