"""
FastAPI app tying the inbound webhook/WS pipeline and the outbound
trigger routes together. Routes are thin per CURSOR.md — each one resolves
the provider then calls into orchestrator/auth/ownership/outbound/
idempotency/callctx; none of the actual logic lives here. `/health` stays
undepended so the docker-compose healthcheck still passes (lesson 1); the
WS route also takes no Depends (Latency section: zero new work on the
connected-call path).
"""

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

import httpx
from fastapi import Depends, FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from websockets.exceptions import ConnectionClosed

from libs.media_stream_sdk.bridge import MediaStreamBridge
from libs.media_stream_sdk.serializers import CloudonixSerializer, VobizSerializer
from libs.telephony_sdk import providers as _providers  # noqa: F401 — registers every built-in provider

from . import auth, idempotency, orchestrator, outbound
from .accounts import accounts
from .auth import require_telephony_caller
from .health import health_loop
from .ownership import OwnershipError, resolve_outbound_identity
from .outbound import OutboundUnavailable

log = logging.getLogger("telephony.app")

_REFRESH_S = float(os.environ.get("TELEPHONY_REFRESH_S", "300"))
CAMPAIGNS_SERVICE_URL = os.environ.get("CAMPAIGNS_SERVICE_URL", "http://localhost:8400").rstrip("/")

_SERIALIZERS = {"vobiz": VobizSerializer, "cloudonix": CloudonixSerializer}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await accounts.refresh()
    refresh_task = asyncio.create_task(accounts.refresh_loop(_REFRESH_S))
    health_task = asyncio.create_task(health_loop())
    yield
    refresh_task.cancel()
    health_task.cancel()


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

    fields = await orchestrator.combined_fields(request)
    idem_key = request.query_params.get("idem")
    event = request.query_params.get("event")  # "ring" or "hangup" — we set this ourselves when building the URL
    lowered = {k.lower(): v for k, v in fields.items()}
    call_id = lowered.get("calluuid") or lowered.get("call_uuid") or lowered.get("callsid") or lowered.get("call_sid")

    if idem_key and call_id:
        await idempotency.note_reference(provider, uuid.UUID(account.tenant_id), idem_key, str(call_id))

    # ring_url and hangup_url share this route; only hangup is terminal.
    if event == "hangup":
        await _notify_campaigns_best_effort(call_id, lowered)
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


async def _notify_campaigns_best_effort(call_id: str | None, fields: dict) -> None:
    if not call_id:
        return
    status = str(fields.get("status") or fields.get("callstatus") or "").lower()
    answered = status not in ("no-answer", "no_answer", "failed", "busy") if status else True
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{CAMPAIGNS_SERVICE_URL}/internal/vobiz-call-resolved",
                json={"call_uuid": call_id, "succeeded": answered, "detail": status or "answered"},
            )
    except httpx.HTTPError:
        log.exception("telephony: failed to notify campaign service of call=%s outcome", call_id)


# ── Outbound (internal caller-facing) ────────────────────────────────────


class PlaceCallBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_slug: str
    agent_slug: str
    to: str
    from_: str = Field(alias="from")
    idempotency_key: str


class SmsBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_slug: str
    to: str
    from_: str = Field(alias="from")
    text: str
    idempotency_key: str


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
    # Same {"ok": ..., "call_uuid"/"error": ...} shape place_call's own
    # response uses — a poller must not learn a second, differently-shaped
    # body depending on whether it asked at claim-time or via this replay.
    status_code, body = outbound.result_from_outcome(entry)
    return _json_response(status_code, body)


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
