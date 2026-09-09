"""
services/toolexec/executor.py — the chain runner (T11-T15). Steps, per the
design's "Chain execution semantics":

  1-3 (T11): verify ownership in one query, resolve the execution order
      (graph.resolve_order, an independent runtime backstop over
      chain_levels), admit-or-refuse (admission.py), and claim the run row
      (conditional insert, loser's path — lesson 8).
  4   (T12): per-step outbound call — re-validate the endpoint (SSRF/DNS
      rebinding), pin the connection to the pre-validated address, cap the
      response read.
  5   (T13): resolve and PLACE arguments (never concatenate) with header/
      path injection guards.
  6   (T14): the side-effect fail-closed claim — one HMAC derivation, two
      domain-separation tags, atomic conditional insert before the call.
  7-8 (T15): apply tenant credentials at call time, persist each step
      redacted, finalize the run, and interpolate success_template from
      the redacted projection only.

Every DB write in this module is its own auto-committed statement, never
part of a transaction spanning an outbound HTTP call — holding a
transaction open across network I/O is what would make the run claim and
the side-effect claim invisible to a genuinely concurrent second request
until commit, defeating the reason they are conditional inserts at all.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from libs.config_sdk.secret_resolver import CompositeSecretResolver

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

async def _verify_ownership(tenant_id: str, agent_id: str, api_name: str) -> dict | None:
    """The single ownership-verification query. tenant_id/agent_id/api_name
    all arrive as independent, untrusted body fields — this is what stops
    a caller pairing one tenant's tenant_id with another tenant's agent_id,
    and ca.deleted_at IS NULL / a.deleted_at IS NULL are load-bearing: a
    soft-deleted API (T7) or agent is unexecutable the very next turn with
    no cascade write, purely because this JOIN can no longer produce a row
    (lesson 16).

    Column note: the task's query is `SELECT ca.*, atp.timeout_ms,
    atp.max_chain_depth` — custom_apis ALSO has its own timeout_ms column,
    so an unaliased atp.timeout_ms collides with ca.timeout_ms under the
    same name. asyncpg's dict(row) keeps only the LAST of two same-named
    columns, which would silently replace the API's own per-step ceiling
    with the agent policy override every time one exists. The two atp.*
    columns are aliased below so both survive dict(row) intact; every
    JOIN, WHERE and deleted_at filter is unchanged from the task's query."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT ca.*, atp.timeout_ms AS agent_policy_timeout_ms, "
        "       atp.max_chain_depth AS agent_policy_max_chain_depth "
        "FROM agents a "
        "JOIN custom_apis ca        ON ca.tenant_id = a.tenant_id AND lower(ca.name) = lower($3) "
        "                           AND ca.deleted_at IS NULL "
        "JOIN agent_custom_apis aca ON aca.agent_id = a.id AND aca.custom_api_id = ca.id AND aca.enabled "
        "LEFT JOIN agent_tool_policies atp ON atp.agent_id = a.id AND atp.tool_name = 'execute_api' "
        "                                 AND atp.enabled "
        "WHERE a.id = $2 AND a.tenant_id = $1 AND a.deleted_at IS NULL",
        tenant_id, agent_id, api_name,
    )
    return dict(row) if row is not None else None


async def _build_api_tree(tenant_id: str, target_id: str) -> tuple[dict, dict[str, dict], dict[str, list[dict]]]:
    """Builds the nested graph.resolve_order()-shaped tree for target_id
    fresh from custom_api_params on every call — never from the
    denormalized chain_levels (AC 11 backstop). Returns (tree, api_rows,
    params_by_api) so the caller can look up each node's own row/params
    during execution without re-querying per step."""
    pool = await db.get_pool()
    # dict(row) alone leaves auth_config/sensitive_response_paths as the
    # raw JSON strings asyncpg returns for JSONB (no pool codec — see
    # db.json_col); decoded here via custom_apis' own row decoder so
    # redaction.redact() gets a real list, not a string it would
    # silently iterate character-by-character.
    api_rows = {str(r["id"]): custom_apis_module._decode_custom_api_row(r) for r in await pool.fetch(
        "SELECT * FROM custom_apis WHERE tenant_id = $1 AND deleted_at IS NULL", tenant_id,
    )}
    params_by_api: dict[str, list[dict]] = {}
    for r in await pool.fetch(
        "SELECT p.* FROM custom_api_params p "
        "JOIN custom_apis ca ON ca.id = p.custom_api_id AND ca.deleted_at IS NULL "
        "WHERE ca.tenant_id = $1",
        tenant_id,
    ):
        params_by_api.setdefault(str(r["custom_api_id"]), []).append(custom_apis_module._decode_param_row(r))

    memo: dict[str, dict] = {}

    def _build(api_id: str) -> dict:
        if api_id in memo:
            return memo[api_id]
        node = {"id": api_id, "name": api_rows[api_id]["name"], "upstream_apis": []}
        memo[api_id] = node  # inserted before recursing: makes a stored cycle safe to WALK (graph.resolve_order still refuses it)
        for param in params_by_api.get(api_id, []):
            if param["source"] == "upstream":
                node["upstream_apis"].append(_build(str(param["upstream_api_id"])))
        return node

    return _build(str(target_id)), api_rows, params_by_api


def _compute_levels(order: list[dict]) -> dict[str, int]:
    """order is post-order (every upstream appears before its dependent),
    so levels can be read off directly — leaf = 1, same semantic as
    custom_apis.chain_levels."""
    levels: dict[str, int] = {}
    for node in order:
        ups = node.get("upstream_apis") or []
        levels[node["id"]] = 1 if not ups else 1 + max(levels[u["id"]] for u in ups)
    return levels


async def _claim_run(tenant_id: str, agent_id: str, target_api_id: str, request: ChainExecuteRequest) -> tuple[str | None, dict | None]:
    """Conditional insert, the loser's path (lesson 8) — never a
    SELECT-then-INSERT. Returns (run_id, None) if this call won the claim,
    or (None, existing_run_row) if a run with this (tenant_id,
    idempotency_key) already exists."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
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

    existing = await pool.fetchrow(
        "SELECT * FROM api_chain_runs WHERE tenant_id = $1 AND idempotency_key = $2",
        tenant_id, request.idempotency_key,
    )
    return None, dict(existing) if existing is not None else None


async def _response_from_existing_run(run: dict) -> ChainExecuteResponse:
    """The loser's path when the existing run is already terminal (or
    still running) — reconstructs the response with ZERO new transport
    calls."""
    pool = await db.get_pool()
    if run["status"] == "running":
        return ChainExecuteResponse(run_id=str(run["id"]), chain_status="failed", error="chain_already_running")

    step_rows = await pool.fetch(
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
    """Connects to one of resolve_and_validate_endpoint()'s own
    already-validated allowed_ips rather than letting the transport
    re-resolve DNS itself at connect time (finding 6 — DNS rebinding): a
    validate-then-connect design whose connect step does its own fresh
    lookup can still land on a different, unvalidated address if the
    record changes in between the two. SNI and certificate verification
    stay on the ORIGINAL HOSTNAME via httpcore's `sni_hostname` request
    extension; the Host header httpx already set from the original URL at
    Request-construction time is untouched here. TLS therefore still
    validates a real certificate against the real hostname — verify=False
    is never used and is forbidden outright."""

    def __init__(self, allowed_ips: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._allowed_ips = allowed_ips

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_hostname = request.url.host
        request.extensions["sni_hostname"] = original_hostname
        request.url = request.url.copy_with(host=self._allowed_ips[0])
        return await super().handle_async_request(request)


def _step_transport(allowed_ips: list[str]) -> httpx.AsyncHTTPTransport:
    """Isolated call site so tests can monkeypatch this to inject an
    httpx.MockTransport instead of dialing anything real — same pattern
    services/toolexec/custom_apis.py's _resolve_addresses already uses."""
    return PinnedResolverTransport(allowed_ips)


def _max_response_bytes() -> int:
    return int(os.environ.get(_MAX_RESPONSE_BYTES_ENV, _DEFAULT_MAX_RESPONSE_BYTES))


async def _read_capped(response: httpx.Response) -> tuple[bytes, bool]:
    """Abandons the stream past the cap instead of reading to completion
    first — an unbounded read is both a memory hazard and free egress
    amplification (finding 10). Short-circuits on Content-Length when
    present."""
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
    timeout = httpx.Timeout(step_timeout_ms / 1000)  # explicit on every axis (lesson 18) — never the library default
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
    """Returns (headers, query_params, body_fields, [path url after
    substitution]) — actually returns (headers, query_params, body_fields,
    url) plus argument_sources and from_prior_step via the caller reading
    the same params list. Raises _StepFailure for a missing required
    caller value, an unresolved upstream path, or an injection attempt —
    in every case BEFORE any request is built."""
    headers: dict[str, str] = {}
    query_params: dict[str, Any] = {}
    body_fields: dict[str, Any] = {}
    url = api_row["endpoint_url"]
    argument_sources: dict[str, str] = {}
    from_prior_step: list[str] = []
    missing_fields: list[dict] = []
    raw_values: dict[str, Any] = {}

    # Pass 1: resolve every value from its declared source. All missing
    # required `caller` fields are collected before raising, so
    # missing_fields names every gap in one response rather than one at a
    # time; an unresolvable `upstream` value raises immediately (no
    # request can be built without it regardless of what else is caller
    # or literal).
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
            extracted = graph.extract(upstream_response, json_path) if upstream_response is not None else graph.MISSING
            if extracted is graph.MISSING:
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
        typed_value = _coerce(raw_values[name], param["json_type"])
        location = param["location"]

        if location == "body":
            body_fields[name] = typed_value
            continue

        string_value = typed_value if isinstance(typed_value, str) else str(typed_value)

        if location == "header":
            if not _HEADER_VALUE_RE.fullmatch(string_value):
                # The value is deliberately NOT included in the error —
                # a spoken "\r\nAuthorization: Bearer x" must not itself
                # be echoed back into logs or step rows either.
                raise _StepFailure("invalid_argument", "illegal_header_value")
            headers[name] = string_value
        elif location == "path":
            if ".." in string_value:
                # quote(value, safe="") does NOT percent-encode '.' (it is
                # an RFC3986 unreserved character), so "../../admin/refund"
                # would survive quoting completely unchanged and still
                # read as a parent-directory segment. THIS check — not
                # the quoting below — is what actually blocks it.
                raise _StepFailure("invalid_argument", "illegal_path_value")
            url = url.replace("{" + name + "}", quote(string_value, safe=""))
        elif location == "query":
            query_params[name] = typed_value

    return headers, query_params, body_fields, url, argument_sources, from_prior_step


# ── step 6: side-effect fail-closed claim ─────────────────────────────────

_platform_secret_resolver = CompositeSecretResolver()  # the PLATFORM resolver — never the tenant-namespaced one
_hmac_key_cache: dict[str, bytes] = {}  # keyed by the ref string, so two different refs never share a cached key


async def _get_hmac_key() -> tuple[bytes, str]:
    """Resolved via the platform CompositeSecretResolver — NOT
    auth_schemes' tenant-namespaced resolver, since TOOLEXEC_ARGS_HMAC_KEY_REF
    is a platform secret, not tenant-authored input. Absent -> fails
    loudly the same way JWT_SECRET does. Called eagerly from app.py's
    lifespan so a misconfigured deploy fails to START rather than passing
    /health and then failing on the first real chain; cached by ref for
    the process lifetime, so that startup call and every later call
    inside execute_chain() share the same resolved key without
    re-resolving."""
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
    """ONE derivation function, TWO domain-separation tags: 'claim'
    (stored in api_chain_steps.arguments_hash and
    api_side_effect_claims.arguments_hash) and 'idem' (exported ONLY in
    the downstream idempotency header, custom_apis.idempotency_header).
    Same key, same canonical input, so the stored and exported values
    cannot drift apart — different tag, so neither reveals the other: the
    idempotency header is sent in plaintext to an endpoint the tenant
    admin controls, so a single derivation would hand that admin an HMAC
    oracle over its own argument space (finding 7).

    Rotation: a key rotation blinds both the platform claim and the
    downstream idempotency dedupe for one TOOLEXEC_SIDE_EFFECT_CLAIM_TTL
    window, since both derive from the same secret."""
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
    """asyncpg's interval codec requires an actual datetime.timedelta, not
    a bare string, even when the SQL parameter is cast `::interval` — the
    cast happens server-side, after client-side encoding already needs the
    right Python type."""
    amount_str, _, unit = value.strip().partition(" ")
    unit = unit.strip().lower()
    if not unit or unit not in _INTERVAL_UNIT_SECONDS:
        raise ValueError(f"unrecognized {_CLAIM_TTL_ENV} value: {value!r}")
    return datetime.timedelta(seconds=float(amount_str) * _INTERVAL_UNIT_SECONDS[unit])


def _claim_ttl() -> datetime.timedelta:
    return _parse_interval(os.environ.get(_CLAIM_TTL_ENV, _DEFAULT_CLAIM_TTL))


async def _claim_side_effect(tenant_id: str, custom_api_id: str, arguments_hash: str, run_id: str, session_id: str) -> bool:
    """The atomic conditional insert (lesson 8) taken BEFORE the outbound
    call — never a read-then-write. Zero rows = the loser's path: a live
    claim already exists, so this step must not fire the call again."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
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
    """Called only on proof no mutation occurred (a non-409 4xx) — frees
    an immediate legitimate retry. Timeout/5xx never call this: the claim
    stays 'claimed' (fail closed — a timed-out mutation may well have
    landed)."""
    pool = await db.get_pool()
    await pool.execute(
        "UPDATE api_side_effect_claims SET status = 'released' "
        "WHERE tenant_id = $1 AND custom_api_id = $2 AND arguments_hash = $3",
        tenant_id, custom_api_id, arguments_hash,
    )


async def _mark_side_effect_success(tenant_id: str, custom_api_id: str, arguments_hash: str) -> None:
    """A genuine 2xx sets the claim 'success' (distinct from merely
    'claimed') — reclaimable only after the TTL window, same as 'claimed',
    but recorded distinctly so chain history can tell a timed-out claim
    apart from one that is known to have actually gone through."""
    pool = await db.get_pool()
    await pool.execute(
        "UPDATE api_side_effect_claims SET status = 'success' "
        "WHERE tenant_id = $1 AND custom_api_id = $2 AND arguments_hash = $3",
        tenant_id, custom_api_id, arguments_hash,
    )


# ── steps 7-8: auth, persistence, success_template ────────────────────────

_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]")


def _interpolate_success_template(template: str, redacted_response: dict) -> str | None:
    """Reads every placeholder value ONLY from the already-redacted
    projection — never the raw response — so a path made sensitive since
    the template was registered is caught here too, not just at
    registration. A placeholder is unresolved, and suppresses the WHOLE
    template (returns None rather than a partially-filled or literal
    '{{...}}' string), in EITHER of two cases:

      - the path is genuinely absent from this response, or
      - the path IS present but its value is the redaction sentinel
        "[redacted]" — speaking that sentinel back as if it were a real
        value ("your SSN on file is [redacted]") both confirms to the
        caller that a sensitive field exists and reads as a malfunction;
        the LLM narrates from `data` instead, which itself still shows
        "[redacted]" as an ordinary field value rather than a spoken
        confirmation of anything.
    """
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

    # Reuses T8's agent_apis._effective_max_chain_depth — the SAME notion
    # of "effective ceiling" the enable-time gate already applies, so the
    # two can never disagree (an API allowed to enable is also allowed to
    # run, and vice versa). It already folds in graph.MAX_CHAIN_LEVELS
    # (NULL override = platform ceiling; a set override can only LOWER
    # it), so the request's own max_chain_depth is min'd against it here,
    # never against the platform ceiling alone.
    effective_ceiling = await agent_apis._effective_max_chain_depth(agent_id)
    max_levels = min(request.max_chain_depth, effective_ceiling)
    tree, api_rows, params_by_api = await _build_api_tree(tenant_id, target_api_id)
    try:
        order = graph.resolve_order(tree, max_levels)
    except ValueError as exc:
        return ChainExecuteResponse(run_id="", chain_status="failed", error=str(exc))

    chain_budget_ms = min(request.chain_budget_ms, int(os.environ.get(_MAX_CHAIN_BUDGET_MS_ENV, _DEFAULT_MAX_CHAIN_BUDGET_MS)))

    if not admission.acquire(tenant_id, agent_id):
        return ChainExecuteResponse(run_id="", chain_status="rate_limited", error="rate_limited")

    try:
        run_id, existing_run = await _claim_run(tenant_id, agent_id, target_api_id, request)
        if run_id is None:
            # The loser's path — no new work, no new transport call.
            admission.release(tenant_id, agent_id)
            if existing_run is None:
                return ChainExecuteResponse(run_id="", chain_status="failed", error="chain_already_running")
            return await _response_from_existing_run(existing_run)

        return await _run_steps(
            run_id=run_id, tenant_id=tenant_id, agent_id=agent_id, request=request,
            order=order, api_rows=api_rows, params_by_api=params_by_api,
            chain_budget_ms=chain_budget_ms,
        )
    finally:
        # Only reached for the winning path (the loser already released
        # above and returned) — held until the run reaches a terminal
        # status, including the barge-in case where the chain keeps
        # running server-side (lesson 26: nothing here is un-torn-down,
        # this IS the teardown).
        if run_id is not None:
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

        # Defined before the try block so the except handler can always
        # redact and persist whatever was resolved so far, even if the
        # failure happened before every one of these was assigned.
        headers: dict[str, str] = {}
        query_params: dict[str, Any] = {}
        body_fields: dict[str, Any] = {}
        argument_sources: dict[str, str] | None = None
        arguments_hash: str | None = None
        started = time.monotonic()

        try:
            headers, query_params, body_fields, url, argument_sources, from_prior_step = _resolve_arguments(
                api_row, params_by_api.get(api_id, []), request.caller_arguments, prior_responses,
            )

            hostname, allowed_ips = await resolve_and_validate_endpoint(api_row["endpoint_url"])

            try:
                await auth_schemes.apply(api_row, headers, query_params)
            except ValueError:
                # auth_schemes.apply() already never puts the ref itself in
                # its own ValueError — re-raised here as the step outcome
                # the design specifies, still with no ref anywhere in it.
                raise _StepFailure("unavailable", "credential_unavailable")

            resolved_arguments = {**body_fields, **query_params, **headers}
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
                # Fail closed on a side-effecting step: keep the claim (do
                # NOT release) since a timed-out mutation may well have landed.
                raise _StepFailure("timeout", "step_timeout")

            if truncated:
                # Keep the claim here too — the response was abandoned, so
                # whether the mutation landed is genuinely unknown.
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
            arguments_redacted = redaction.redact({**body_fields, **query_params, **headers}, sensitive_param_names)
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
            stop = True

    if failed_step is None:
        chain_status = "success"
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
        failed_step=failed_step, data=data, deterministic_response=deterministic_response, error=chain_error,
    )


async def _finalize_run(run_id: str, chain_status: str, error: str | None) -> None:
    pool = await db.get_pool()
    await pool.execute(
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
    await pool.execute(
        "INSERT INTO api_chain_steps "
        "(run_id, step_index, custom_api_id, api_name, level, session_id, status, http_status, error, "
        " arguments_redacted, response_redacted, argument_sources, arguments_hash, side_effecting, "
        " idempotency_key, duration_ms) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12::jsonb, $13, $14, $15, $16)",
        run_id, step_index, api_row["id"], api_row["name"], level, session_id, status, http_status, error,
        json.dumps(arguments_redacted) if arguments_redacted is not None else None,
        json.dumps(response_redacted) if response_redacted is not None else None,
        json.dumps(argument_sources) if argument_sources is not None else None,
        arguments_hash, bool(api_row["side_effecting"]), idempotency_key, duration_ms,
    )
