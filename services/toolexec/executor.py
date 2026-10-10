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

from . import admission, agent_apis, auth_schemes, db, graph, oauth, presets, redaction
from . import custom_apis as custom_apis_module
from .custom_apis import PinnedResolverTransport, resolve_and_validate_endpoint
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


async def _remote_party(tenant_id: str, request: ChainExecuteRequest) -> str | None:
    """The number of the party on the other end of this call, or None when the
    call metadata cannot name one. The only place the request's call numbers
    are read. A number that is one of this tenant's own DIDs is refused too:
    that is what an outbound call mislabelled `inbound` presents, with the
    campaign DID as its caller."""
    number = presets.remote_party_number(request.call_direction, request.caller_number, request.called_number)
    if number is None:
        return None
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        is_own_did = await conn.fetchval(
            "SELECT 1 FROM phone_numbers "
            "WHERE tenant_id = $1 AND '+' || regexp_replace(did, '\\D', '', 'g') = $2 LIMIT 1",
            tenant_id, number,
        )
    return None if is_own_did else number


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


def _set_body_path(body: dict, path: str, value: Any) -> None:
    """Places `value` at a dotted path ('start.dateTime', 'values.0.1'): a
    digit segment is a list index, any other a dict key. A list slot skipped
    over (an optional field left out) is filled with None."""
    segments = path.split(".")
    node: Any = body
    for segment, following in zip(segments, segments[1:]):
        default: Any = [] if following.isdigit() else {}
        if isinstance(node, list):
            index = int(segment)
            node.extend([None] * (index + 1 - len(node)))
            if node[index] is None:
                node[index] = default
            node = node[index]
        else:
            node = node.setdefault(segment, default)
    last = segments[-1]
    if isinstance(node, list):
        index = int(last)
        node.extend([None] * (index + 1 - len(node)))
        node[index] = value
    else:
        node[last] = value


def _resolve_arguments(
    api_row: dict, params: list[dict], caller_arguments: dict, prior_responses: dict[str, Any],
    *, endpoint_url: str, remote_party: str | None,
) -> tuple[dict, dict, dict, str, dict, list[str], dict]:
    """Returns (headers, query_params, body_fields, url, argument_sources,
    from_prior_step, path_values). Raises _StepFailure for a missing required
    caller value, an unresolved upstream path, or an injection attempt —
    in every case BEFORE any request is built.

    path_values holds each path param by its bare name: the value is part of
    what the step does (which event a cancel deletes), so the caller hashes
    and redacts it with the rest of the arguments.

    A `caller_id` param is the call's remote party, from `remote_party` and
    nowhere else: `caller_arguments` is never read for it, so nothing the model
    says can change who a booking or a message is for."""
    headers: dict[str, str] = {}
    query_params: dict[str, Any] = {}
    body_fields: dict[str, Any] = {}
    path_values: dict[str, str] = {}
    url = endpoint_url
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
        elif source == "caller_id":
            if remote_party is None:
                raise _StepFailure("invalid_argument", "caller_id_unavailable")
            number = presets.digits_only(remote_party) if param["value_digits_only"] else remote_party
            raw_values[name] = (param["value_prefix"] or "") + number
            argument_sources[name] = "caller_id"
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
            if param.get("body_path"):
                _set_body_path(body_fields, param["body_path"], typed_value)
            else:
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
            path_values[name] = string_value
        elif location == "query":
            query_params[name] = typed_value

    return headers, query_params, body_fields, url, argument_sources, from_prior_step, path_values


def _redaction_keys(params: list[dict]) -> list[str]:
    """What redaction.redact matches a `sensitive` param by: its bare name, or
    for one placed at a body_path its JSON path, since the outbound body (and
    so the persisted arguments) holds it nested, not under its name."""
    def key(param: dict) -> str:
        if not param.get("body_path"):
            return param["name"]
        return "$" + "".join(
            f"[{segment}]" if segment.isdigit() else f".{segment}" for segment in param["body_path"].split(".")
        )

    return [key(p) for p in params if p.get("sensitive")]


def _form_fields(body_fields: dict) -> dict:
    """A form body can only carry scalars: a nested value goes out as its JSON text."""
    return {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in body_fields.items()}


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


async def _claim_side_effect(
    tenant_id: str, custom_api_id: str, arguments_hash: str, run_id: str, session_id: str,
) -> uuid.UUID | None:
    """The atomic conditional insert (lesson 8) taken BEFORE the outbound
    call — never a read-then-write. None = the loser's path: a live claim
    already exists, so this step must not fire the call again. Otherwise the
    claim row's id."""
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
    return row["id"] if row is not None else None


_CONFIRMATION_WINDOW = "10 minutes"


class _ConfirmationRequired(Exception):
    """The step is gated and this is its first call for these arguments: the
    read-back goes to the caller and nothing is dispatched."""

    def __init__(self, read_back: str) -> None:
        super().__init__("confirmation_required")
        self.read_back = read_back


async def _confirmed_in_prior_turn(
    tenant_id: str, session_id: str, custom_api_id: str, arguments_hash: str, turn_id: str,
) -> bool:
    """True when this session already read these exact arguments back in an
    EARLIER turn (so a caller utterance came in between, `turn_id` being a fresh
    uuid per turn) and no successful dispatch has used that read-back since.
    The NOT EXISTS makes each read-back good for one dispatch, so a cancel
    followed by a rebook of the same slot has to be confirmed again."""
    if not session_id:
        return False  # nothing to scope a confirmation to
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM api_chain_steps s "
            "JOIN api_chain_runs r ON r.id = s.run_id "
            "WHERE r.tenant_id = $1 AND r.session_id = $2 AND s.custom_api_id = $3 "
            "  AND s.arguments_hash = $4 AND s.status = 'confirmation_required' "
            "  AND r.turn_id <> $5 AND s.created_at > now() - $6::interval "
            "  AND NOT EXISTS (SELECT 1 FROM api_chain_steps d "
            "                    JOIN api_chain_runs dr ON dr.id = d.run_id "
            "                   WHERE dr.tenant_id = $1 AND dr.session_id = $2 AND d.custom_api_id = $3 "
            "                     AND d.arguments_hash = $4 AND d.status = 'success' "
            "                     AND d.created_at > s.created_at) "
            "LIMIT 1",
            tenant_id, session_id, custom_api_id, arguments_hash, turn_id, _parse_interval(_CONFIRMATION_WINDOW),
        )
    return row is not None


async def _sends_in_session(tenant_id: str, custom_api_id: str, session_id: str) -> int:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM api_chain_steps s JOIN api_chain_runs r ON r.id = s.run_id "
            "WHERE r.tenant_id = $1 AND s.custom_api_id = $2 AND s.session_id = $3 AND s.status = 'success'",
            tenant_id, custom_api_id, session_id,
        )


async def _release_booking_claim(
    tenant_id: str, preset_key: str, target_api_name: str, event_id: str,
) -> bool:
    """Frees the claim of the booking a successful cancel just deleted, so the
    slot can be booked again this session. The booking's claim id IS the event
    id (it was written into the event as its `id`), so `event_id` names exactly
    one claim. DELETE, not a status flip: the rebook's insert then mints a fresh
    id, where a kept row would hand it the cancelled event's id, which Google
    refuses to reuse. An event_id that is not a uuid hex (an event the tenant
    made by hand) raises before any SQL."""
    claim_id = uuid.UUID(hex=event_id)
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "DELETE FROM api_side_effect_claims c USING custom_apis b "
            "WHERE c.tenant_id = $1 AND c.id = $2 AND c.status = 'success' "
            "  AND b.tenant_id = $1 AND b.id = c.custom_api_id AND b.preset_key = $3 AND b.name = $4 "
            "RETURNING c.id",
            tenant_id, claim_id, preset_key, target_api_name,
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
    read_back: str | None = None
    final_redacted_response: dict | None = None
    remaining_budget_ms = chain_budget_ms
    stop = False

    # Once per chain, and only for a chain that has a use for it.
    needs_remote_party = any(
        p["source"] == "caller_id" for node in order for p in params_by_api.get(node["id"], [])
    ) or any(
        (api_rows[node["id"]].get("response_transform") or {}).get("kind") == "google_booking_lookup"
        for node in order
    )
    remote_party = await _remote_party(tenant_id, request) if needs_remote_party else None

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
        path_values: dict[str, str] = {}
        argument_sources: dict[str, str] | None = None
        arguments_hash: str | None = None
        injected_auth_keys: set[str] = set()
        started = time.monotonic()

        try:
            effective_url = api_row["endpoint_url"]
            if api_row["endpoint_base_source"] == "oauth_connection":
                api_base = await oauth.connection_api_base(tenant_id, api_row["oauth_connection_id"])
                if api_base is None:
                    raise _StepFailure("unavailable", "reconnect_required")
                effective_url = api_base + api_row["endpoint_url"]

            headers, query_params, body_fields, url, argument_sources, from_prior_step, path_values = _resolve_arguments(
                api_row, params_by_api.get(api_id, []), request.caller_arguments, prior_responses,
                endpoint_url=effective_url, remote_party=remote_party,
            )

            hostname, allowed_ips = await resolve_and_validate_endpoint(url)

            try:
                injected_auth_keys = await auth_schemes.apply(api_row, headers, query_params, effective_url=url)
            except auth_schemes.ReconnectRequired:
                raise _StepFailure("unavailable", "reconnect_required")
            except ValueError:
                raise _StepFailure("unavailable", "credential_unavailable")

            # Exclude auth-injected keys: never persist credentials, and rotating tokens would break the hash dedupe.
            resolved_arguments = {
                k: v for k, v in {**body_fields, **query_params, **headers, **path_values}.items()
                if k not in injected_auth_keys
            }
            arguments_redacted = redaction.redact(resolved_arguments, _redaction_keys(params_by_api.get(api_id, [])))
            side_effecting = bool(api_row["side_effecting"])
            idempotency_key_value = None
            if side_effecting:
                arguments_hash = _derive("claim", tenant_id, api_id, resolved_arguments, key, kid)
                if api_row.get("confirmation_template") and not await _confirmed_in_prior_turn(
                    tenant_id, request.session_id, api_id, arguments_hash, request.turn_id,
                ):
                    upstream_responses = {
                        api_rows[u]["name"]: prior_responses[u]
                        for u in {str(p["upstream_api_id"]) for p in params_by_api.get(api_id, []) if p["source"] == "upstream"}
                    }
                    try:
                        read_back = presets.render_confirmation(
                            api_row["confirmation_template"], arguments_redacted, upstream_responses,
                        )
                    except ValueError:
                        raise _StepFailure("failed", "confirmation_unrenderable") from None
                    raise _ConfirmationRequired(read_back)
                if api_row.get("session_send_cap") and (
                    not request.session_id
                    or await _sends_in_session(tenant_id, api_id, request.session_id) >= api_row["session_send_cap"]
                ):
                    # `unavailable`, never `invalid_argument`: that tells the model to retry with
                    # different arguments, which is exactly how a recipient gets changed.
                    raise _StepFailure("unavailable", "send_cap_reached")
                idempotency_key_value = _derive("idem", tenant_id, api_id, resolved_arguments, key, kid)
                if api_row.get("idempotency_header"):
                    headers[api_row["idempotency_header"]] = idempotency_key_value
                claimed = await _claim_side_effect(tenant_id, api_id, arguments_hash, run_id, request.session_id)
                if not claimed:
                    raise _StepFailure("failed", "side_effecting_step_already_completed")
                if api_row.get("idempotency_body_field"):
                    # The claim's own id: a cancel deletes the claim, so a rebook gets a fresh one
                    # (Google keeps a deleted event's id and refuses to reuse it).
                    body_fields[api_row["idempotency_body_field"]] = claimed.hex

            step_timeout_ms = min(api_row.get("timeout_ms") or 6000, remaining_budget_ms)
            body_style = api_row.get("body_style", "json")
            json_body = body_fields if body_style == "json" and body_fields else None
            data_body = _form_fields(body_fields) if body_style == "form" and body_fields else None

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
                if target := presets.claim_release_target(api_row):
                    # The cancel already happened upstream: failing to free the slot must not
                    # turn it into a reported failure. The rebook is then refused until the TTL.
                    try:
                        await _release_booking_claim(tenant_id, api_row["preset_key"], target, path_values["event_id"])
                    except Exception:
                        log.warning("booking_claim_release_failed", extra={"custom_api_id": api_id})

            if api_row.get("response_transform"):
                # Before anything below sees the response: a projection that drops fields
                # keeps them out of the step row, the success template and the model.
                try:
                    raw_response = presets.apply_response_transform(
                        api_row["response_transform"], raw_response, body_fields, caller_ani=remote_party,
                    )
                except ValueError:
                    raise _StepFailure("failed", "response_transform_failed") from None

            prior_responses[api_id] = raw_response
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

        except _ConfirmationRequired as gate:
            duration_ms = int((time.monotonic() - started) * 1000)
            await _persist_step(
                run_id, step_index, api_row, levels[api_id], request.session_id,
                status="confirmation_required", http_status=None, error=None,
                arguments_redacted=arguments_redacted, response_redacted=None,
                argument_sources=argument_sources, arguments_hash=arguments_hash,
                idempotency_key=None, duration_ms=duration_ms,
            )
            steps.append(ChainStepReport(
                api_name=api_name, level=levels[api_id], status="confirmation_required",
                from_prior_step=from_prior_step,
            ))
            read_back = gate.read_back
            chain_error = "confirmation_required"
            stop = True

        except _StepFailure as failure:
            duration_ms = int((time.monotonic() - started) * 1000)
            failure_arguments = {
                k: v for k, v in {**body_fields, **query_params, **headers, **path_values}.items()
                if k not in injected_auth_keys
            }
            arguments_redacted = redaction.redact(failure_arguments, _redaction_keys(params_by_api.get(api_id, [])))
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

    if read_back is not None:
        chain_status = "confirmation_required"
    elif failed_step is None:
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

    deterministic_response = read_back
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
