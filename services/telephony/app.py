"""Telephony FastAPI app: inbound webhook/WS pipeline and outbound trigger routes (thin; logic lives elsewhere).
`/health` and the WS route take no Depends (healthcheck; zero work on the connected-call path)."""

from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(
    _os.path.dirname(__file__), "..", "conversation", "generated",
))

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Path, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from websockets.exceptions import ConnectionClosed

from libs.media_stream_sdk.bridge import MediaStreamBridge
from libs.media_stream_sdk.serializers import CloudonixSerializer, VobizSerializer
from libs.telephony_sdk import campaign_events
from libs.telephony_sdk import providers as _providers  # noqa: F401 — registers every built-in provider

from . import auth, idempotency, orchestrator, outbound
from .accounts import accounts
from .auth import require_telephony_caller
from .health import health_loop
from .ownership import OwnershipError, resolve_outbound_identity
from .outbound import OutboundUnavailable

log = logging.getLogger("telephony.app")

_REFRESH_S = float(os.environ.get("TELEPHONY_REFRESH_S", "300"))
_NONCE_TTL_S = 86400

_SERIALIZERS = {"vobiz": VobizSerializer, "cloudonix": CloudonixSerializer}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await accounts.refresh()
    refresh_task = asyncio.create_task(accounts.refresh_loop(_REFRESH_S))
    health_task = asyncio.create_task(health_loop())
    yield
    refresh_task.cancel()
    health_task.cancel()


_publish_tasks: set[asyncio.Task] = set()

app = FastAPI(title="Telephony Service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"loaded": accounts.loaded}


# ── Inbound (vendor-facing, no Depends) ──────────────────────────────────


@app.api_route("/{provider}/voice/{account_ref}", methods=["GET", "POST"])
async def voice(provider: str, account_ref: str, request: Request) -> Response:
    return await orchestrator.handle_inbound_webhook(provider_name=provider, account_ref=account_ref, request=request)


@app.websocket("/{provider}/stream/{token}")
async def stream(websocket: WebSocket, provider: str, token: str) -> None:
    await websocket.accept()
    route = orchestrator.callctx.claim(token)
    if route is None or route.provider != provider:
        log.warning("telephony.stream.unknown_token provider=%s", provider)
        await websocket.close(code=1008)
        return

    serializer_cls = _SERIALIZERS.get(provider)
    if serializer_cls is None:
        log.warning("telephony.stream.unsupported_provider provider=%s", provider)
        await websocket.close(code=1008)
        return

    def on_session_start(session_id: str) -> None:
        orchestrator.call_session_map.put(route.provider_call_id, session_id)
        if route.direction == "outbound" and route.idempotency_key:
            account = accounts.get(route.provider, route.account_ref)
            task = asyncio.create_task(_publish(
                kind="answered", key=route.idempotency_key, tenant_id=account.tenant_id if account else None,
                provider_call_id=route.provider_call_id, call_session_id=session_id, cause="answered", answered=True,
            ))
            _publish_tasks.add(task)
            task.add_done_callback(_publish_tasks.discard)

    bridge = MediaStreamBridge(
        serializer=serializer_cls(),
        call_id=route.provider_call_id,
        tenant_slug=route.tenant_slug,
        agent_slug=route.agent_slug,
        direction=route.direction,
        caller_did=route.caller_did,
        called_did=route.called_did,
        log_name="telephony.bridge",
        on_session_start=on_session_start,
    )
    try:
        await bridge.run(websocket)
        orchestrator.call_session_map.delete(route.provider_call_id)
    except (WebSocketDisconnect, ConnectionClosed):
        log.info("telephony: caller disconnected mid-stream call=%s", route.provider_call_id)


@app.post("/{provider}/status/{account_ref}")
async def status_callback(provider: str, account_ref: str, request: Request) -> Response:
    account = accounts.get(provider, account_ref)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if account is None or not account.instance.verify_webhook_signature(str(request.url), headers):
        return PlainTextResponse("forbidden", status_code=403)

    nonce = next((v for k, v in headers.items() if k.endswith("-nonce")), None)
    if nonce and not await idempotency.claim_webhook_nonce(provider, account_ref, nonce, _NONCE_TTL_S):
        return PlainTextResponse("forbidden", status_code=403)

    fields = await orchestrator.combined_fields(request)
    idem_key = request.query_params.get("idem")
    event = request.query_params.get("event")  # "ring" or "hangup" — we set this ourselves when building the URL
    lowered = {k.lower(): v for k, v in fields.items()}
    call_id = lowered.get("calluuid") or lowered.get("call_uuid") or lowered.get("callsid") or lowered.get("call_sid")

    if idem_key and call_id:
        await idempotency.note_reference(provider, uuid.UUID(account.tenant_id), idem_key, str(call_id))

    # ring_url and hangup_url share this route; only hangup is terminal.
    if event == "hangup":
        status = str(lowered.get("status") or lowered.get("callstatus") or "").lower()
        await _publish(
            kind="hangup", key=idem_key, tenant_id=account.tenant_id, provider_call_id=call_id, call_session_id=None,
            cause=status, answered=status not in ("no-answer", "no_answer", "failed", "busy") if status else True,
            unconfirmed=not account.instance.signature_covers_fields,
        )
    return PlainTextResponse("ok")


@app.post("/{provider}/dtmf/{account_ref}")
async def dtmf_webhook(provider: str, account_ref: str, request: Request) -> Response:
    account = accounts.get(provider, account_ref)
    if account is None:
        return PlainTextResponse("not found", status_code=404)

    try:
        result = await orchestrator.handle_dtmf_webhook(provider, account, request)
        return PlainTextResponse("ok") if result else PlainTextResponse("bad request", status_code=400)
    except Exception as e:
        log.exception("telephony.dtmf.error provider=%s account=%s", provider, account_ref)
        return PlainTextResponse("error", status_code=500)


async def _publish(*, unconfirmed: bool = False, **event) -> None:
    fields = campaign_events.encode(source="cpaas", billable_ms=None, unconfirmed=unconfirmed, **event)
    await idempotency.publish_event(campaign_events.STREAM_CPAAS, fields)


# ── Outbound (internal caller-facing) ────────────────────────────────────


class PlaceCallBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_slug: str
    agent_slug: str
    to: str
    from_: str = Field(alias="from")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,128}$")

    @field_validator("idempotency_key", mode="before")
    def validate_idempotency_key(cls, v: str) -> str:
        if not v or len(v) > 128:
            raise ValueError("idempotency_key must be 1-128 characters")
        if not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("idempotency_key must contain only alphanumeric, underscore, and hyphen characters")
        return v


class ReconcileBody(BaseModel):
    tenant_slug: str
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,128}$")


class SmsBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_slug: str
    to: str
    from_: str = Field(alias="from")
    text: str
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,128}$")

    @field_validator("idempotency_key", mode="before")
    def validate_idempotency_key(cls, v: str) -> str:
        if not v or len(v) > 128:
            raise ValueError("idempotency_key must be 1-128 characters")
        if not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("idempotency_key must contain only alphanumeric, underscore, and hyphen characters")
        return v


@app.post("/{provider}/call")
async def place_call(provider: str, body: PlaceCallBody, user=Depends(require_telephony_caller)) -> Response:
    tenant_id, tenant_slug = await auth.resolve_caller_tenant(user, body.tenant_slug)
    try:
        identity = await resolve_outbound_identity(
            tenant_id=tenant_id, tenant_slug=tenant_slug,
            requested_agent_slug=body.agent_slug, from_number=body.from_,
        )
    except OwnershipError as exc:
        return PlainTextResponse(str(exc), status_code=403)

    try:
        status_code, response_body = await outbound.place_call(
            provider=provider, identity=identity, to_number=body.to, idempotency_key=body.idempotency_key,
        )
    except OutboundUnavailable as exc:
        return PlainTextResponse(str(exc), status_code=503)
    return _json_response(status_code, response_body)


@app.get("/{provider}/call/idempotency/{key}")
async def read_idempotency(
    provider: str, key: str, tenant_slug: str = Query(...), user=Depends(require_telephony_caller),
) -> Response:
    tenant_id, _slug = await auth.resolve_caller_tenant(user, tenant_slug)
    entry = await idempotency.read(provider, tenant_id, key)
    if entry is None:
        return PlainTextResponse("not found", status_code=404)
    if entry.get("state") == "in_flight":
        return _json_response(200, {"status": "pending"})
    # Same body shape as place_call's own response.
    status_code, body = outbound.result_from_outcome(entry)
    return _json_response(status_code, body)


@app.post("/{provider}/call/reconcile")
async def reconcile_call(provider: str, body: ReconcileBody, user=Depends(require_telephony_caller)) -> Response:
    tenant_id, tenant_slug = await auth.resolve_caller_tenant(user, body.tenant_slug)
    try:
        result = await outbound.reconcile(
            provider=provider, tenant_id=tenant_id, tenant_slug=tenant_slug, idempotency_key=body.idempotency_key,
        )
    except OutboundUnavailable as exc:
        return PlainTextResponse(str(exc), status_code=503)
    return _json_response(200, result)


@app.get("/{provider}/call/{call_id}/status")
async def call_status(
    provider: str, call_id: str = Path(pattern=r"^[A-Za-z0-9_\-]{1,128}$"),
    tenant_slug: str = Query(...), user=Depends(require_telephony_caller),
) -> Response:
    _tenant_id, slug = await auth.resolve_caller_tenant(user, tenant_slug)
    try:
        result = await outbound.call_status(provider=provider, tenant_slug=slug, call_id=call_id)
    except OutboundUnavailable as exc:
        return PlainTextResponse(str(exc), status_code=503)
    return _json_response(200, result)


@app.get("/{provider}/outbound-readiness")
async def outbound_readiness(provider: str, tenant_slug: str = Query(...), user=Depends(require_telephony_caller)) -> Response:
    _tenant_id, slug = await auth.resolve_caller_tenant(user, tenant_slug)
    return _json_response(200, await outbound.readiness(provider=provider, tenant_slug=slug))


@app.post("/sms/send")
async def send_sms(body: SmsBody, user=Depends(require_telephony_caller)) -> Response:
    tenant_id, tenant_slug = await auth.resolve_caller_tenant(user, body.tenant_slug)
    try:
        identity = await resolve_outbound_identity(
            tenant_id=tenant_id, tenant_slug=tenant_slug,
            requested_agent_slug=None, from_number=body.from_,
        )
    except OwnershipError as exc:
        return PlainTextResponse(str(exc), status_code=403)

    try:
        status_code, response_body = await outbound.send_sms(
            identity=identity, to_number=body.to, text=body.text, idempotency_key=body.idempotency_key,
        )
    except OutboundUnavailable as exc:
        return PlainTextResponse(str(exc), status_code=503)
    return _json_response(status_code, response_body)


def _json_response(status_code: int, body: dict) -> Response:
    import json as _json

    return Response(content=_json.dumps(body), media_type="application/json", status_code=status_code)
