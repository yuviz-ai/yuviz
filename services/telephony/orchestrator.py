"""
handle_inbound_webhook() — the single provider-agnostic pipeline every
vendor's inbound webhook goes through (AC9). Order is the AC6/AC7/AC11/AC12
contract and must not be reordered — see 02-design.md's Interfaces section
for why rate limiting runs before signature verification (AC12: a failed
signature must still cost quota) and why the tenant is taken from the
account, never the DID (AC8).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse

from libs.ratelimit import FixedWindowCounter
from libs.telephony_sdk import did_route
from libs.telephony_sdk.exceptions import WebhookRejected
from libs.telephony_sdk.interface import NormalizedInboundCall
from libs.telephony_sdk.registry import TelephonyProviderRegistry

from .accounts import Account, accounts
from .callctx import CallContextStore, CallRoute, HandoffCapacityError, outbound_identities

log = logging.getLogger("telephony.orchestrator")


class CallSessionMap:
    def __init__(self):
        self._map: dict[str, str] = {}  # provider_call_id -> session_id
        self._ttl = 3600.0  # 1 hour
        self._timestamps: dict[str, float] = {}

    def put(self, provider_call_id: str, session_id: str) -> None:
        self._map[provider_call_id] = session_id
        self._timestamps[provider_call_id] = time.monotonic()

    def get(self, provider_call_id: str) -> str | None:
        now = time.monotonic()
        if provider_call_id in self._map:
            if now - self._timestamps[provider_call_id] < self._ttl:
                return self._map[provider_call_id]
            else:
                del self._map[provider_call_id]
                del self._timestamps[provider_call_id]
        return None

    def delete(self, provider_call_id: str) -> None:
        self._map.pop(provider_call_id, None)
        self._timestamps.pop(provider_call_id, None)


call_session_map = CallSessionMap()

_ACCOUNT_LIMIT = int(os.environ.get("TELEPHONY_ACCOUNT_LIMIT", "300"))
_DID_LIMIT = int(os.environ.get("TELEPHONY_DID_LIMIT", "30"))

# Authenticated-only buckets, keyed on server-derived values only — an
# unknown account_ref always buckets into "{provider}:unknown", so no real
# account's quota is reachable without its own UUID (Risks: "rate limiting
# moved before authentication").
_per_account = FixedWindowCounter(limit=_ACCOUNT_LIMIT, window_seconds=60)
_per_did = FixedWindowCounter(limit=_DID_LIMIT, window_seconds=60)

callctx = CallContextStore()


@dataclass(frozen=True)
class InboundRoute:
    tenant_slug: str
    agent_slug: str
    caller_did: str
    called_did: str
    provider: str
    provider_call_id: str
    direction: str = "inbound"


async def combined_fields(request: Request) -> dict:
    """query ∪ JSON-or-form body — same shape as
    services/cloudonix/app.py's _combined_fields, generalized for every
    provider (Vobiz's form-encoded webhooks included)."""
    fields: dict = dict(request.query_params)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                fields.update(body)
        except Exception:
            pass
    else:
        try:
            form = await request.form()
            fields.update(dict(form))
        except Exception:
            pass
    return fields


async def resolve_inbound_route(call: NormalizedInboundCall, account: Account) -> InboundRoute | None:
    """The tenant is NEVER derived from the DID — it is call.known_tenant_slug
    when the adapter filled it, and account.tenant_slug otherwise. did_route
    only selects the agent: a miss yields "default"; a hit whose tenant
    differs from the account's tenant returns None (foreign_did, AC8)."""
    tenant_slug = call.known_tenant_slug or account.tenant_slug

    route = await did_route.resolve_did_route(call.to_number)
    if route is None:
        agent_slug = "default"
    else:
        hit_tenant, hit_agent = route
        if hit_tenant != tenant_slug:
            log.warning(
                "telephony.reject.foreign_did provider=%s account=%s hit_tenant=%s",
                account.provider, account.account_ref, hit_tenant,
            )
            return None
        agent_slug = hit_agent

    return InboundRoute(
        tenant_slug=tenant_slug, agent_slug=agent_slug,
        caller_did=call.from_number, called_did=call.to_number,
        provider=account.provider, provider_call_id=call.provider_call_id,
    )


async def handle_inbound_webhook(*, provider_name: str, account_ref: str, request: Request) -> Response:
    # Step 1: unknown provider -> 404, before anything else (AC10). Provider
    # names are public constants, not tenant-owned (lesson 2).
    try:
        provider_cls = TelephonyProviderRegistry.get(provider_name)
    except ValueError:
        return PlainTextResponse("not found", status_code=404)

    # Step 2: "no key map means admit nothing" (matches cloudonix/app.py today).
    if not accounts.loaded:
        log.warning("telephony.reject.not_loaded provider=%s account=%s", provider_name, account_ref)
        return PlainTextResponse("service unavailable", status_code=503)

    account = accounts.get(provider_name, account_ref)
    bucket_ref = account_ref if account is not None else "unknown"

    # Step 3: rate limit on server-derived values only, BEFORE signature —
    # increment() runs unconditionally so a signature failure consumes
    # quota identically to a valid request (AC12).
    over, retry_after = _per_account.over_limit(f"{provider_name}:{bucket_ref}")
    _per_account.increment(f"{provider_name}:{bucket_ref}")
    if over:
        log.warning("telephony.reject.rate_limit provider=%s account=%s", provider_name, bucket_ref)
        return PlainTextResponse("too many requests", status_code=429)

    # Step 4: signature, on the instance built from THIS account's
    # decrypted credentials. Unknown account or a failed check -> 403,
    # identical body/status for both (AC7).
    headers = {k.lower(): v for k, v in request.headers.items()}
    if account is None or not account.instance.verify_webhook_signature(str(request.url), headers):
        log.info("telephony.reject.signature provider=%s account=%s known=%s", provider_name, account_ref, account is not None)
        return PlainTextResponse("forbidden", status_code=403)

    fields = await combined_fields(request)
    try:
        call = account.instance.normalize_inbound_webhook(
            url=str(request.url), headers=headers, fields=fields, account_tenant_slug=account.tenant_slug,
        )
    except WebhookRejected:
        log.info("telephony.reject.webhook provider=%s account=%s", provider_name, account_ref)
        return PlainTextResponse("forbidden", status_code=403)

    # Step 6: second, DID-scoped rate limit — needs the parsed body.
    over, _retry = _per_did.over_limit(f"{provider_name}:{account_ref}:{call.to_number}")
    _per_did.increment(f"{provider_name}:{account_ref}:{call.to_number}")
    if over:
        log.warning("telephony.reject.rate_limit_did provider=%s account=%s", provider_name, account_ref)
        return PlainTextResponse("too many requests", status_code=429)

    # Step 7: route resolution. A call this service itself placed carries
    # its own `?idem=` back on answer_url/hangup_url/ring_url (outbound.
    # place_call() sets it and remembers the identity under the same key
    # before ever dialling) — that identity is used directly, skipping DID
    # resolution entirely, because DID resolution is for a genuinely
    # inbound call: run against an outbound leg's CALLEE number it either
    # silently downgrades the answering agent to "default" (the real
    # agent_slug validated at trigger time is never consulted) or 403s with
    # dead air whenever that callee number happens to be provisioned as
    # another tenant's DID.
    idem_key = request.query_params.get("idem")
    outbound_identity = outbound_identities.recall(provider_name, account_ref, idem_key) if idem_key else None
    if outbound_identity is None:
        # Cloudonix has no per-call ?idem= — fall back to the vendor's call_id.
        outbound_identity = outbound_identities.recall(provider_name, account_ref, call.provider_call_id)
    if outbound_identity is not None:
        tenant_slug, agent_slug = outbound_identity
        route = InboundRoute(
            tenant_slug=tenant_slug, agent_slug=agent_slug,
            caller_did=call.from_number, called_did=call.to_number,
            provider=account.provider, provider_call_id=call.provider_call_id,
            direction="outbound",
        )
    else:
        route = await resolve_inbound_route(call, account)
        if route is None:
            return PlainTextResponse("forbidden", status_code=403)

    # Step 8: context issue.
    try:
        token = callctx.issue(CallRoute(
            tenant_slug=route.tenant_slug, agent_slug=route.agent_slug,
            caller_did=route.caller_did, called_did=route.called_did,
            provider=account.provider, account_ref=account.account_ref,
            provider_call_id=route.provider_call_id, issued_at=time.monotonic(),
            direction=route.direction,
        ))
    except HandoffCapacityError:
        log.warning("telephony.reject.capacity provider=%s account=%s", provider_name, account_ref)
        return PlainTextResponse("service unavailable", status_code=503)

    ws_base = _ws_base()
    xml = account.instance.build_answer_response(f"{ws_base}/{provider_name}/stream/{token}")
    return Response(content=xml, media_type="application/xml")


def _ws_base() -> str:
    base = os.environ["TELEPHONY_PUBLIC_BASE_URL"].rstrip("/")
    return base.replace("https://", "wss://").replace("http://", "ws://")


async def handle_dtmf_webhook(provider_name: str, account: Account, request: Request) -> bool:
    try:
        provider_cls = TelephonyProviderRegistry.get(provider_name)
    except ValueError:
        return False

    fields = await combined_fields(request)
    headers = {k.lower(): v for k, v in request.headers.items()}

    if not account.instance.verify_webhook_signature(str(request.url), headers):
        log.info("telephony.dtmf.reject.signature provider=%s account=%s", provider_name, account.account_ref)
        return False

    try:
        dtmf_digit = account.instance.parse_dtmf_digit(fields)
        provider_call_id = fields.get("call_id") or fields.get("callid") or fields.get("call_uuid") or fields.get("callsid")
        if not dtmf_digit or not provider_call_id:
            log.warning("telephony.dtmf.missing_fields provider=%s account=%s", provider_name, account.account_ref)
            return False

        session_id = call_session_map.get(provider_call_id)
        if not session_id:
            log.warning("telephony.dtmf.unknown_call provider=%s account=%s call_id=%s", provider_name, account.account_ref, provider_call_id)
            return False

        await _send_dtmf_to_conversation_service(session_id, dtmf_digit)
        log.info("telephony.dtmf.sent provider=%s account=%s session=%s digit=%s", provider_name, account.account_ref, session_id, dtmf_digit)
        return True
    except Exception as e:
        log.exception("telephony.dtmf.error provider=%s account=%s", provider_name, account.account_ref)
        return False


async def _send_dtmf_to_conversation_service(session_id: str, digit: str) -> None:
    import asyncio
    import grpc
    from voiceai.v1 import conversation_pb2, conversation_pb2_grpc

    channel_addr = os.environ.get("CONVERSATION_SERVICE_GRPC", "localhost:50051")
    try:
        async with grpc.aio.insecure_channel(channel_addr) as channel:
            stub = conversation_pb2_grpc.ConversationServiceStub(channel)
            call = stub.Converse()

            dtmf_msg = conversation_pb2.GatewayMessage(
                dtmf=conversation_pb2.DtmfDigit(session_id=session_id, digit=digit)
            )
            await call.write(dtmf_msg)

            try:
                await asyncio.wait_for(call.done_writing(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        log.warning("telephony.dtmf.grpc_error session=%s digit=%s error=%s", session_id, digit, str(e))
