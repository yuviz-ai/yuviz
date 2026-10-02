"""Custom API chain runner: ownership, ordering, admission, pinned outbound calls, side-effect claims.

Every DB write is auto-committed; a transaction spanning HTTP I/O would hide claims from concurrent requests.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import uuid
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from libs.config_sdk.secret_resolver import CompositeSecretResolver
from libs.tenancy import tenant_conn

from . import admission, agent_apis, auth_schemes, db, graph, redaction
from . import custom_apis as custom_apis_module
from .custom_apis import resolve_and_validate_endpoint
from .schemas import ChainExecuteRequest, ChainExecuteResponse, ChainStepReport

log = logging.getLogger(__name__)

_MAX_CHAIN_BUDGET_MS_ENV = "TOOLEXEC_MAX_CHAIN_BUDGET_MS"
_DEFAULT_MAX_CHAIN_BUDGET_MS = 30_000

_MAX_RESPONSE_BYTES_ENV = "TOOLEXEC_MAX_RESPONSE_BYTES"
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024

_CLAIM_TTL_ENV = "TOOLEXEC_SIDE_EFFECT_CLAIM_TTL"
_DEFAULT_CLAIM_TTL = "24 hours"

_HMAC_KEY_REF_ENV = "TOOLEXEC_ARGS_HMAC_KEY_REF"
_HMAC_KEY_ID_ENV = "TOOLEXEC_ARGS_HMAC_KEY_ID"
_DEFAULT_HMAC_KEY_ID = "k1"

_HEADER_VALUE_RE = re.compile(r"^[\x20-\x7E]*$")


# ── step 1-3: ownership, ordering, admission, run claim ──────────────────

async def _resolve_tenant_uuid(conn: Any, tenant_id: str) -> str | None:
    """Resolve a wire `tenant_id` (slug or UUID) to a UUID for raw SQL comparisons."""
    try:
        uuid.UUID(tenant_id)
        return tenant_id
    except (ValueError, AttributeError, TypeError):
        pass
    row = await conn.fetchrow(
        "SELECT id FROM tenants WHERE slug = $1 AND deleted_at IS NULL", tenant_id,
    )
    return str(row["id"]) if row is not None else None


async def _verify_ownership(tenant_id: str, agent_id: str, api_name: str) -> dict | None:
    """One query binding untrusted tenant/agent/api together; deleted_at filters make soft-deletes immediate.

    atp.* columns are aliased: dict(row) would otherwise let atp.timeout_ms overwrite ca.timeout_ms.
    """
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        resolved_tenant_id = await _resolve_tenant_uuid(conn, tenant_id)
        if resolved_tenant_id is None:
            return None
        row = await conn.fetchrow(
            "SELECT ca.*, atp.timeout_ms AS agent_policy_timeout_ms, "
            "       atp.max_chain_depth AS agent_policy_max_chain_depth "
            "FROM agents a "
            "JOIN custom_apis ca        ON ca.tenant_id = a.tenant_id AND lower(ca.name) = lower($3) "
            "                           AND ca.deleted_at IS NULL "
            "JOIN agent_custom_apis aca ON aca.agent_id = a.id AND aca.custom_api_id = ca.id AND aca.enabled "
            "LEFT JOIN agent_tool_policies atp ON atp.agent_id = a.id AND atp.tool_name = 'execute_api' "
            "                                 AND atp.enabled "
            "WHERE a.id = $2 AND a.tenant_id = $1 AND a.deleted_at IS NULL",
            resolved_tenant_id, agent_id, api_name,
        )
    return dict(row) if row is not None else None


class _UnbuildableApi(Exception):
    """An api this chain touches has an undecodable row or param; refuse rather than dispatch with holes."""

    def __init__(self, api_id: str) -> None:
        super().__init__(f"custom_api {api_id} could not be decoded")
        self.api_id = api_id


async def _build_api_tree(tenant_id: str, target_id: str) -> tuple[dict, dict[str, dict], dict[str, list[dict]]]:
    """Return (tree, api_rows, params_by_api) built fresh from params, never from chain_levels.

    Raises _UnbuildableApi only if an api reachable from target_id is undecodable.
    """
    pool = await db.get_pool()
    # Decoded row by row so one malformed row only breaks chains that reach it.
    api_rows: dict[str, dict] = {}
    poisoned: set[str] = set()
    async with tenant_conn(pool) as conn:
        api_query_rows = await conn.fetch(
            "SELECT * FROM custom_apis WHERE tenant_id = $1 AND deleted_at IS NULL", tenant_id,
        )
    for r in api_query_rows:
        try:
            api_rows[str(r["id"])] = custom_apis_module._decode_custom_api_row(r)
        except RuntimeError:
            log.exception("malformed custom_apis row %s in tenant %s", r["id"], tenant_id)
            poisoned.add(str(r["id"]))

    params_by_api: dict[str, list[dict]] = {}
    async with tenant_conn(pool) as conn:
        param_query_rows = await conn.fetch(
            "SELECT p.* FROM custom_api_params p "
            "JOIN custom_apis ca ON ca.id = p.custom_api_id AND ca.deleted_at IS NULL "
            "WHERE ca.tenant_id = $1",
            tenant_id,
        )
    for r in param_query_rows:
        try:
            decoded = custom_apis_module._decode_param_row(r)
        except RuntimeError:
            # Poison the whole api, not just this param.
            log.exception("malformed custom_api_params row %s in tenant %s", r["id"], tenant_id)
            poisoned.add(str(r["custom_api_id"]))
            continue
        params_by_api.setdefault(str(r["custom_api_id"]), []).append(decoded)

    memo: dict[str, dict] = {}

    def _build(api_id: str) -> dict:
        if api_id in memo:
            return memo[api_id]
        if api_id in poisoned or api_id not in api_rows:
            raise _UnbuildableApi(api_id)
        node = {"id": api_id, "name": api_rows[api_id]["name"], "upstream_apis": []}
        memo[api_id] = node  # inserted before recursing: makes a stored cycle safe to WALK (graph.resolve_order still refuses it)
        for param in params_by_api.get(api_id, []):
            if param["source"] == "upstream":
                node["upstream_apis"].append(_build(str(param["upstream_api_id"])))
        return node

    return _build(str(target_id)), api_rows, params_by_api


def _compute_levels(order: list[dict]) -> dict[str, int]:
    """Levels from a post-order list (leaf = 1, same as chain_levels)."""
    levels: dict[str, int] = {}
    for node in order:
        ups = node.get("upstream_apis") or []
        levels[node["id"]] = 1 if not ups else 1 + max(levels[u["id"]] for u in ups)
    return levels


async def _claim_run(tenant_id: str, agent_id: str, target_api_id: str, request: ChainExecuteRequest) -> tuple[str | None, dict | None]:
    """Conditional insert; returns (run_id, None) on win or (None, existing_run) on conflict."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO api_chain_runs "
            "(tenant_id, agent_id, call_id, session_id, turn_id, tool_call_id, idempotency_key, target_api_id, status) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'running') "
            "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING "
            "RETURNING id",
            tenant_id, agent_id, request.call_id, request.session_id, request.turn_id,
            request.tool_call_id, request.idempotency_key, target_api_id,
        )
        if row is not None:
            return str(row["id"]), None

        existing = await conn.fetchrow(
            "SELECT * FROM api_chain_runs WHERE tenant_id = $1 AND idempotency_key = $2",
            tenant_id, request.idempotency_key,
        )
    return None, dict(existing) if existing is not None else None


async def _response_from_existing_run(run: dict) -> ChainExecuteResponse:
    """Rebuild the response for an existing run without any new outbound calls."""
    pool = await db.get_pool()
    if run["status"] == "running":
        return ChainExecuteResponse(run_id=str(run["id"]), chain_status="failed", error="chain_already_running")

    async with tenant_conn(pool) as conn:
        step_rows = await conn.fetch(
            "SELECT * FROM api_chain_steps WHERE run_id = $1 ORDER BY step_index", run["id"],
        )
    steps = [
        ChainStepReport(
            api_name=r["api_name"], level=r["level"], status=r["status"],
            from_prior_step=[k for k, v in (db.json_col(r["argument_sources"]) or {}).items() if ":" in str(v)],
        )
        for r in step_rows
    ]
    completed = [s.api_name for s in steps if s.status == "success"]
    failed_step = next((s for s in steps if s.status not in ("success", "skipped")), None)
    data = {}
    if run["status"] == "success" and step_rows:
        data = db.json_col(step_rows[-1]["response_redacted"]) or {}
    return ChainExecuteResponse(
        run_id=str(run["id"]), chain_status=run["status"], steps=steps,
        completed_steps=completed, failed_step=failed_step, data=data, error=run["error"],
    )


# ── step 4: pinned outbound transport + capped response read ─────────────

class PinnedResolverTransport(httpx.AsyncHTTPTransport):
    """Dial a pre-validated IP (no re-resolve, defeats DNS rebinding); SNI/cert stay on the original host.

    Never use verify=False.
    """

    def __init__(self, allowed_ips: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._allowed_ips = allowed_ips

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_hostname = request.url.host
        request.extensions["sni_hostname"] = original_hostname
        request.url = request.url.copy_with(host=self._allowed_ips[0])
        return await super().handle_async_request(request)


def _step_transport(allowed_ips: list[str]) -> httpx.AsyncHTTPTransport:
    """Separate function so tests can inject an httpx.MockTransport."""
    return PinnedResolverTransport(allowed_ips)


def _max_response_bytes() -> int:
    return int(os.environ.get(_MAX_RESPONSE_BYTES_ENV, _DEFAULT_MAX_RESPONSE_BYTES))


async def _read_capped(response: httpx.Response) -> tuple[bytes, bool]:
    """Read up to the cap, abandoning the stream past it; returns (body, truncated)."""
    cap = _max_response_bytes()
    content_length = response.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > cap:
        await response.aclose()
        return b"", True

    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            truncated = True
            break
    await response.aclose()
    return b"".join(chunks), truncated


async def _do_request(
    method: str, url: str, headers: dict, query_params: dict,
    json_body: dict | None, data_body: dict | None, allowed_ips: list[str], step_timeout_ms: int,
) -> tuple[int, bytes, bool]:
    timeout = httpx.Timeout(step_timeout_ms / 1000)  # explicit on every axis, never the library default
    transport = _step_transport(allowed_ips)
    async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False) as client:
        async with client.stream(
            method, url, headers=headers, params=query_params, json=json_body, data=data_body,
        ) as response:
            body, truncated = await _read_capped(response)
            return response.status_code, body, truncated


# ── step 5: argument resolution and placement (never concatenation) ──────

class _StepFailure(Exception):
    def __init__(
        self, status: str, error: str, missing_fields: list[dict] | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.missing_fields = missing_fields or []
        self.http_status = http_status


def _coerce(value: Any, json_type: str) -> Any:
    if value is None:
        return None
    if json_type == "string":
        return value if isinstance(value, str) else str(value)
    if json_type == "integer":
        return int(value)
    if json_type == "number":
        return float(value)
    if json_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return bool(value)
    return value  # object/array — passed through as-is


def _resolve_arguments(
    api_row: dict, params: list[dict], caller_arguments: dict, prior_responses: dict[str, Any],
) -> tuple[dict, dict, dict, list[str]]:
    """Return (headers, query_params, body_fields, url, argument_sources, from_prior_step).

    Raises _StepFailure on missing/unresolved/injected values before any request is built.
    """
    headers: dict[str, str] = {}
    query_params: dict[str, Any] = {}
    body_fields: dict[str, Any] = {}
    url = api_row["endpoint_url"]
    argument_sources: dict[str, str] = {}
    from_prior_step: list[str] = []
    missing_fields: list[dict] = []
    raw_values: dict[str, Any] = {}

    # Pass 1: resolve values; collect all missing caller fields before raising.
    for param in params:
        name = param["name"]
        source = param["source"]

        if source == "literal":
            raw_values[name] = param["literal_value"]
            argument_sources[name] = "literal"
        elif source == "caller":
            if name not in caller_arguments:
                if param.get("required", True):
                    missing_fields.append({"name": name, "description": param.get("description", "")})
                continue
            raw_values[name] = caller_arguments[name]
            argument_sources[name] = "caller"
        else:  # upstream
            upstream_id = str(param["upstream_api_id"])
            upstream_name = prior_responses.get("__names__", {}).get(upstream_id, upstream_id)
            json_path = param["upstream_json_path"]
            upstream_response = prior_responses.get(upstream_id)
            if upstream_response is None:
                extracted, miss_reason = graph.MISSING, graph.PATH_ABSENT
            else:
                extracted, miss_reason = graph.extract_with_reason(upstream_response, json_path)
            if extracted is graph.MISSING:
                # NO_MATCH is a normal "nothing found" answer; PATH_ABSENT is a config bug.
                if miss_reason == graph.NO_MATCH:
                    raise _StepFailure(
                        "invalid_argument", "upstream_no_match",
                        [{"name": name, "description": (
                            f"{upstream_name} found no match for what the caller gave. "
                            f"Tell them plainly that nothing was found, and ask for a "
                            f"different spelling or more detail rather than guessing.")}],
                    )
                raise _StepFailure("failed", "upstream_value_missing")
            raw_values[name] = extracted
            argument_sources[name] = f"{upstream_name}:{json_path}"
            from_prior_step.append(name)

    if missing_fields:
        raise _StepFailure("invalid_argument", "missing_fields", missing_fields)

    # Pass 2: coerce and PLACE each resolved value — never concatenated
    # into a URL, header block, or query string.
    for param in params:
        name = param["name"]
        if name not in argument_sources:
            continue  # an optional caller field that was simply absent
        try:
            typed_value = _coerce(raw_values[name], param["json_type"])
        except (TypeError, ValueError):
            # Must be a step outcome: escaping would leave the run stuck 'running' and wedge retries.
            raise _StepFailure(
                "invalid_argument", "invalid_argument_type",
                [{"name": name, "description": param.get("description", "")}],
            )
        location = param["location"]

        if location == "body":
            body_fields[name] = typed_value
            continue

        string_value = typed_value if isinstance(typed_value, str) else str(typed_value)

        if location == "header":
            if not _HEADER_VALUE_RE.fullmatch(string_value):
                # Value deliberately omitted so injected headers aren't echoed into logs.
                raise _StepFailure("invalid_argument", "illegal_header_value")
            headers[name] = string_value
        elif location == "path":
            if ".." in string_value:
                # quote() leaves '.' unencoded, so this check is what blocks traversal.
                raise _StepFailure("invalid_argument", "illegal_path_value")
            url = url.replace("{" + name + "}", quote(string_value, safe=""))
        elif location == "query":
            query_params[name] = typed_value

    return headers, query_params, body_fields, url, argument_sources, from_prior_step


# ── step 6: side-effect fail-closed claim ─────────────────────────────────

_platform_secret_resolver = CompositeSecretResolver()  # the PLATFORM resolver — never the tenant-namespaced one
_hmac_key_cache: dict[str, bytes] = {}  # keyed by the ref string, so two different refs never share a cached key


async def _get_hmac_key() -> tuple[bytes, str]:
    """Resolve the platform HMAC key (cached by ref); raises if unset so startup fails loudly."""
    ref = os.environ.get(_HMAC_KEY_REF_ENV, "").strip()
    if not ref:
        raise RuntimeError(
            f"{_HMAC_KEY_REF_ENV} is not set — side-effect claims and downstream "
            "idempotency keys cannot be derived."
        )
    if ref not in _hmac_key_cache:
        resolved = await _platform_secret_resolver.resolve(ref)
        _hmac_key_cache[ref] = resolved.encode()
    kid = os.environ.get(_HMAC_KEY_ID_ENV, _DEFAULT_HMAC_KEY_ID)
    return _hmac_key_cache[ref], kid


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _derive(tag: str, tenant_id: str, custom_api_id: str, resolved_arguments: dict, key: bytes, kid: str) -> str:
    """HMAC with domain-separation tag ('claim' stored, 'idem' sent downstream) so neither reveals the other.

    Key rotation blinds both dedupes for one claim-TTL window.
    """
    canonical = _canonical_json({"t": str(tenant_id), "a": str(custom_api_id), "args": resolved_arguments})
    digest = hmac.new(key, (tag + "|" + canonical).encode(), hashlib.sha256).hexdigest()
    return f"{kid}:{digest}"


_INTERVAL_UNIT_SECONDS = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
}


def _parse_interval(value: str) -> datetime.timedelta:
    """Parse '24 hours' into a timedelta; asyncpg won't encode a string for ::interval."""
    amount_str, _, unit = value.strip().partition(" ")
    unit = unit.strip().lower()
    if not unit or unit not in _INTERVAL_UNIT_SECONDS:
        raise ValueError(f"unrecognized {_CLAIM_TTL_ENV} value: {value!r}")
    return datetime.timedelta(seconds=float(amount_str) * _INTERVAL_UNIT_SECONDS[unit])


def _claim_ttl() -> datetime.timedelta:
    return _parse_interval(os.environ.get(_CLAIM_TTL_ENV, _DEFAULT_CLAIM_TTL))


async def _claim_side_effect(tenant_id: str, custom_api_id: str, arguments_hash: str, run_id: str, session_id: str) -> bool:
    """Atomic claim taken before the call; False means a live claim exists, so don't fire."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "INSERT INTO api_side_effect_claims "
            "(tenant_id, custom_api_id, arguments_hash, run_id, session_id, status) "
            "VALUES ($1, $2, $3, $4, $5, 'claimed') "
            "ON CONFLICT (tenant_id, custom_api_id, arguments_hash) DO UPDATE "
            "SET run_id = EXCLUDED.run_id, session_id = EXCLUDED.session_id, "
            "    status = 'claimed', claimed_at = now() "
            "WHERE api_side_effect_claims.status = 'released' "
            "   OR api_side_effect_claims.claimed_at < now() - $6::interval "
            "RETURNING id",
            tenant_id, custom_api_id, arguments_hash, run_id, session_id, _claim_ttl(),
        )
    return row is not None


async def _release_side_effect_claim(tenant_id: str, custom_api_id: str, arguments_hash: str) -> None:
    """Release only on proof of no mutation (non-409 4xx); timeouts/5xx stay claimed."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "UPDATE api_side_effect_claims SET status = 'released' "
            "WHERE tenant_id = $1 AND custom_api_id = $2 AND arguments_hash = $3",
            tenant_id, custom_api_id, arguments_hash,
        )


async def _mark_side_effect_success(tenant_id: str, custom_api_id: str, arguments_hash: str) -> None:
    """Mark a 2xx claim 'success' (still TTL-bound) to distinguish it from a timed-out claim."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "UPDATE api_side_effect_claims SET status = 'success' "
            "WHERE tenant_id = $1 AND custom_api_id = $2 AND arguments_hash = $3",
            tenant_id, custom_api_id, arguments_hash,
        )


# ── steps 7-8: auth, persistence, success_template ────────────────────────

_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]")


def _interpolate_success_template(template: str, redacted_response: dict) -> str | None:
    """Fill placeholders from the redacted response only; None if any is absent or redacted."""
    def _resolve(match: "re.Match[str]") -> str:
        raw_path = match.group(1).strip()
        value = graph.extract(redacted_response, raw_path)
        if value is graph.MISSING or value == redaction.REDACTED:
            raise _StepFailure("failed", "unresolved_placeholder")
        text = _CONTROL_CHAR_RE.sub("", str(value))
        return text[:120]

    try:
        return _TEMPLATE_PLACEHOLDER_RE.sub(_resolve, template)
    except _StepFailure:
        return None


async def execute_chain(request: ChainExecuteRequest) -> ChainExecuteResponse:
    # ── steps 1-3 ──
    verified = await _verify_ownership(request.tenant_id, request.agent_id, request.api_name)
    if verified is None:
        return ChainExecuteResponse(run_id="", chain_status="invalid_argument", error="api_not_enabled_for_agent")

    tenant_id = str(verified["tenant_id"])  # from the VERIFIED row, never the request body
    agent_id = request.agent_id  # verified to exist by a.id = $2 in the WHERE clause above
    target_api_id = str(verified["id"])

    # Same ceiling the enable-time gate uses, so enable and run can't disagree.
    effective_ceiling = await agent_apis._effective_max_chain_depth(agent_id)
    max_levels = min(request.max_chain_depth, effective_ceiling)
    try:
        tree, api_rows, params_by_api = await _build_api_tree(tenant_id, target_api_id)
    except _UnbuildableApi as exc:
        log.error("chain for %s refused: %s", target_api_id, exc)
        return ChainExecuteResponse(run_id="", chain_status="failed", error="custom_api_row_undecodable")
    try:
        order = graph.resolve_order(tree, max_levels)
    except ValueError as exc:
        return ChainExecuteResponse(run_id="", chain_status="failed", error=str(exc))

    chain_budget_ms = min(request.chain_budget_ms, int(os.environ.get(_MAX_CHAIN_BUDGET_MS_ENV, _DEFAULT_MAX_CHAIN_BUDGET_MS)))

    if not admission.acquire(tenant_id, agent_id):
        return ChainExecuteResponse(run_id="", chain_status="rate_limited", error="rate_limited")

    # Tracked separately from run_id: _claim_run can raise before run_id is set.
    run_id: str | None = None
    admission_released = False
    try:
        run_id, existing_run = await _claim_run(tenant_id, agent_id, target_api_id, request)
        if run_id is None:
            admission.release(tenant_id, agent_id)
            admission_released = True
            if existing_run is None:
                return ChainExecuteResponse(run_id="", chain_status="failed", error="chain_already_running")
            return await _response_from_existing_run(existing_run)

        return await _run_steps(
            run_id=run_id, tenant_id=tenant_id, agent_id=agent_id, request=request,
            order=order, api_rows=api_rows, params_by_api=params_by_api,
            chain_budget_ms=chain_budget_ms,
        )
    finally:
        # Slot held until the run is terminal, even if the caller barged in.
        if not admission_released:
            admission.release(tenant_id, agent_id)


async def _run_steps(
    *, run_id: str, tenant_id: str, agent_id: str, request: ChainExecuteRequest,
    order: list[dict], api_rows: dict[str, dict], params_by_api: dict[str, list[dict]],
    chain_budget_ms: int,
) -> ChainExecuteResponse:
    levels = _compute_levels(order)
    key, kid = await _get_hmac_key()

    prior_responses: dict[str, Any] = {"__names__": {n["id"]: n["name"] for n in order}}
    steps: list[ChainStepReport] = []
    completed_steps: list[str] = []
    failed_step: ChainStepReport | None = None
    chain_error: str | None = None
    missing_fields: list[dict] = []
    final_redacted_response: dict | None = None
    remaining_budget_ms = chain_budget_ms
    stop = False

    for step_index, node in enumerate(order):
        api_id = node["id"]
        api_row = api_rows[api_id]
        api_name = api_row["name"]

        if stop:
            steps.append(ChainStepReport(api_name=api_name, level=levels[api_id], status="skipped"))
            await _persist_step(
                run_id, step_index, api_row, levels[api_id], request.session_id,
                status="skipped", http_status=None, error=None,
                arguments_redacted=None, response_redacted=None, argument_sources=None,
                arguments_hash=None, idempotency_key=None, duration_ms=None,
            )
            continue

        # Defined before try so the failure path can persist partial state.
        headers: dict[str, str] = {}
        query_params: dict[str, Any] = {}
        body_fields: dict[str, Any] = {}
        argument_sources: dict[str, str] | None = None
        arguments_hash: str | None = None
        injected_auth_keys: set[str] = set()
        started = time.monotonic()

        try:
            headers, query_params, body_fields, url, argument_sources, from_prior_step = _resolve_arguments(
                api_row, params_by_api.get(api_id, []), request.caller_arguments, prior_responses,
            )

            hostname, allowed_ips = await resolve_and_validate_endpoint(api_row["endpoint_url"])

            try:
                injected_auth_keys = await auth_schemes.apply(api_row, headers, query_params)
            except ValueError:
                raise _StepFailure("unavailable", "credential_unavailable")

            # Exclude auth-injected keys: never persist credentials, and rotating tokens would break the hash dedupe.
            resolved_arguments = {
                k: v for k, v in {**body_fields, **query_params, **headers}.items()
                if k not in injected_auth_keys
            }
            side_effecting = bool(api_row["side_effecting"])
            idempotency_key_value = None
            if side_effecting:
                arguments_hash = _derive("claim", tenant_id, api_id, resolved_arguments, key, kid)
                idempotency_key_value = _derive("idem", tenant_id, api_id, resolved_arguments, key, kid)
                if api_row.get("idempotency_header"):
                    headers[api_row["idempotency_header"]] = idempotency_key_value
                claimed = await _claim_side_effect(tenant_id, api_id, arguments_hash, run_id, request.session_id)
                if not claimed:
                    raise _StepFailure("failed", "side_effecting_step_already_completed")

            step_timeout_ms = min(api_row.get("timeout_ms") or 6000, remaining_budget_ms)
            body_style = api_row.get("body_style", "json")
            json_body = body_fields if body_style == "json" and body_fields else None
            data_body = body_fields if body_style == "form" and body_fields else None

            try:
                status_code, body, truncated = await asyncio.wait_for(
                    _do_request(api_row["method"], url, headers, query_params, json_body, data_body,
                                allowed_ips, step_timeout_ms),
                    timeout=step_timeout_ms / 1000,
                )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                # Keep the claim: a timed-out mutation may have landed.
                raise _StepFailure("timeout", "step_timeout")

            if truncated:
                # Keep the claim: whether the mutation landed is unknown.
                raise _StepFailure("failed", "response_too_large")

            if side_effecting and status_code != 409 and 400 <= status_code < 500:
                await _release_side_effect_claim(tenant_id, api_id, arguments_hash)
            # else (409, 2xx/3xx, or 5xx): keep the claim — fail closed.

            try:
                raw_response = json.loads(body) if body else {}
            except ValueError:
                raw_response = {"_raw": body.decode("utf-8", errors="replace")}

            if not (200 <= status_code < 300):
                raise _StepFailure("failed", f"http_status_{status_code}", http_status=status_code)

            if side_effecting:
                await _mark_side_effect_success(tenant_id, api_id, arguments_hash)

            prior_responses[api_id] = raw_response
            sensitive_param_names = [p["name"] for p in params_by_api.get(api_id, []) if p.get("sensitive")]
            arguments_redacted = redaction.redact(resolved_arguments, sensitive_param_names)
            response_redacted = redaction.redact(raw_response, api_row.get("sensitive_response_paths") or [])
            final_redacted_response = response_redacted

            duration_ms = int((time.monotonic() - started) * 1000)
            await _persist_step(
                run_id, step_index, api_row, levels[api_id], request.session_id,
                status="success", http_status=status_code, error=None,
                arguments_redacted=arguments_redacted, response_redacted=response_redacted,
                argument_sources=argument_sources, arguments_hash=arguments_hash,
                idempotency_key=idempotency_key_value, duration_ms=duration_ms,
            )
            steps.append(ChainStepReport(api_name=api_name, level=levels[api_id], status="success",
                                          from_prior_step=from_prior_step))
            completed_steps.append(api_name)
            remaining_budget_ms -= duration_ms

        except _StepFailure as failure:
            duration_ms = int((time.monotonic() - started) * 1000)
            sensitive_param_names = [p["name"] for p in params_by_api.get(api_id, []) if p.get("sensitive")]
            failure_arguments = {
                k: v for k, v in {**body_fields, **query_params, **headers}.items()
                if k not in injected_auth_keys
            }
            arguments_redacted = redaction.redact(failure_arguments, sensitive_param_names)
            await _persist_step(
                run_id, step_index, api_row, levels[api_id], request.session_id,
                status=failure.status, http_status=failure.http_status, error=failure.error,
                arguments_redacted=arguments_redacted, response_redacted=None,
                argument_sources=argument_sources, arguments_hash=arguments_hash,
                idempotency_key=None, duration_ms=duration_ms,
            )
            report = ChainStepReport(api_name=api_name, level=levels[api_id], status=failure.status)
            steps.append(report)
            failed_step = report
            chain_error = failure.error
            missing_fields = failure.missing_fields
            stop = True

    if failed_step is None:
        chain_status = "success"
    elif chain_error == "upstream_no_match":
        # Not "partial": the chain ran fine and nothing matched; keeps missing_fields for the agent.
        chain_status = "invalid_argument"
    elif completed_steps:
        chain_status = "partial"
    elif failed_step.status in ("failed", "timeout", "invalid_argument", "unavailable"):
        chain_status = failed_step.status
    else:
        chain_status = "failed"

    data = final_redacted_response if (chain_status == "success" and final_redacted_response is not None) else {}

    deterministic_response = None
    if chain_status == "success" and final_redacted_response is not None and order:
        template = api_rows[order[-1]["id"]].get("success_template")
        if template:
            deterministic_response = _interpolate_success_template(template, final_redacted_response)

    await _finalize_run(run_id, chain_status, chain_error)

    return ChainExecuteResponse(
        run_id=run_id, chain_status=chain_status, steps=steps, completed_steps=completed_steps,
        failed_step=failed_step, data=data, missing_fields=missing_fields,
        deterministic_response=deterministic_response, error=chain_error,
    )


async def _finalize_run(run_id: str, chain_status: str, error: str | None) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "UPDATE api_chain_runs SET status = $2, error = $3, finished_at = now() WHERE id = $1",
            run_id, chain_status, error,
        )


async def _persist_step(
    run_id: str, step_index: int, api_row: dict, level: int, session_id: str, *,
    status: str, http_status: int | None, error: str | None,
    arguments_redacted: dict | None, response_redacted: dict | None,
    argument_sources: dict | None, arguments_hash: str | None,
    idempotency_key: str | None, duration_ms: int | None,
) -> None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        await conn.execute(
            "INSERT INTO api_chain_steps "
            "(run_id, step_index, custom_api_id, api_name, level, session_id, status, http_status, error, "
            " arguments_redacted, response_redacted, argument_sources, arguments_hash, side_effecting, "
            " idempotency_key, duration_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12::jsonb, $13, $14, $15, $16)",
            run_id, step_index, api_row["id"], api_row["name"], level, session_id, status, http_status, error,
            json.dumps(arguments_redacted) if arguments_redacted is not None else None,
            json.dumps(response_redacted) if response_redacted is not None else None,
            json.dumps(argument_sources) if argument_sources is not None else None,
            # side_effecting means "this row is hash-keyed" (DB constraint), not the API's flag.
            arguments_hash, arguments_hash is not None, idempotency_key, duration_ms,
        )
