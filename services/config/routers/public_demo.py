"""Unauthenticated website demo for one configured agent: a single-use voice credential, or a live chat.

Every route is a CORS "simple" request from the marketing site (no preflight; chat bodies are sent as
text/plain), so each mirrors Access-Control-Allow-Origin itself, and only for PUBLIC_DEMO_ORIGINS.
"""

from __future__ import annotations

import ipaddress
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from libs.ratelimit import FixedWindowCounter
from libs.tenancy import set_target_tenant

from .. import agent_testing, cache
from .. import agents as agents_service
from .. import tenants as tenants_service
from .agents import _agent_channel, _secret_resolver

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])


def _client_key(host: str) -> str:
    """An IPv6 host owns its whole /64, so per-address keys would not limit it."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


class PublicDemoThrottle:
    """Hourly caps on demo passes: one for the whole process, one per client (IPv6 by /64)."""

    _ALL = "all"

    def __init__(self) -> None:
        self.total = FixedWindowCounter(limit=int(os.environ.get("PUBLIC_DEMO_PER_HOUR") or 60), window_seconds=3600)
        self.per_client = FixedWindowCounter(
            limit=int(os.environ.get("PUBLIC_DEMO_PER_IP_PER_HOUR") or 3), window_seconds=3600)

    def allow(self, client_host: str) -> tuple[bool, int]:
        key = _client_key(client_host)
        for counter, k in ((self.total, self._ALL), (self.per_client, key)):
            over, retry_after = counter.over_limit(k)
            if over:
                return False, retry_after
        self.total.increment(self._ALL)
        self.per_client.increment(key)
        return True, 0


class PublicChatThrottle(PublicDemoThrottle):
    """Hourly caps on new website chat sessions; each session is also capped at PUBLIC_CHAT_TURNS."""

    def __init__(self) -> None:
        self.total = FixedWindowCounter(
            limit=int(os.environ.get("PUBLIC_DEMO_CHAT_PER_HOUR") or 120), window_seconds=3600)
        self.per_client = FixedWindowCounter(
            limit=int(os.environ.get("PUBLIC_DEMO_CHAT_PER_IP_PER_HOUR") or 5), window_seconds=3600)


PUBLIC_CHAT_TURNS = 12
_MAX_CHAT_BODY = 4096


class PublicChatTurn(BaseModel):
    credential: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)


def _allowed_origins() -> set[str]:
    return {o.strip() for o in os.environ.get("PUBLIC_DEMO_ORIGINS", "").split(",") if o.strip()}


def _demo_target() -> tuple[str, str] | None:
    tenant, _, agent = (os.environ.get("PUBLIC_DEMO_AGENT") or "").strip().partition("/")
    return (tenant, agent) if tenant and agent else None


def _reply(status: int, body: dict, origin: str, extra: dict[str, str] | None = None) -> JSONResponse:
    headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin", **(extra or {})}
    return JSONResponse(status_code=status, content=body, headers=headers)


async def _load_demo_agent() -> tuple[dict, dict] | None:
    target = _demo_target()
    if target is None:
        return None
    tenant_slug, agent_slug = target
    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        return None
    set_target_tenant(str(tenant["id"]))
    agent = await agents_service.get_agent(tenant_slug, agent_slug)
    if agent is None or agent.get("status") != "active":
        return None
    return tenant, agent


def _client_host(request: Request) -> str:
    # client.host is the socket peer; behind a reverse proxy this needs trusted X-Forwarded-For handling.
    return request.client.host if request.client else "unknown"


def _throttled(exc: HTTPException, origin: str) -> JSONResponse:
    return _reply(429, {"detail": "too many requests; try again later"}, origin, dict(exc.headers or {}))


@router.post("/demo-call")
async def create_demo_call(request: Request):
    origin = request.headers.get("origin", "")
    if origin not in _allowed_origins():
        return JSONResponse(status_code=403, content={"detail": "origin not allowed"})
    if _demo_target() is None:
        return _reply(404, {"detail": "demo not available"}, origin)

    allowed, retry_after = request.app.state.public_demo_throttle.allow(_client_host(request))
    if not allowed:
        return _reply(429, {"detail": "too many demo calls; try again later"}, origin,
                      {"Retry-After": str(retry_after)})

    found = await _load_demo_agent()
    if found is None or _agent_channel(found[1]) != "voice":
        return _reply(404, {"detail": "demo not available"}, origin)
    tenant, agent = found

    minted = await agent_testing.mint_test_session(cache.get_client(), tenant=tenant, agent=agent, channel="voice")
    log.info("public demo: pass issued for %s/%s", tenant["slug"], agent["slug"])
    return _reply(200, {"credential": minted["credential"], "expires_in": minted["expires_in"],
                        "tenant": tenant["slug"], "agent": agent["slug"]}, origin)


@router.post("/demo-chat")
async def create_demo_chat(request: Request):
    origin = request.headers.get("origin", "")
    if origin not in _allowed_origins():
        return JSONResponse(status_code=403, content={"detail": "origin not allowed"})
    if _demo_target() is None:
        return _reply(404, {"detail": "demo not available"}, origin)

    allowed, retry_after = request.app.state.public_chat_throttle.allow(_client_host(request))
    if not allowed:
        return _reply(429, {"detail": "too many demo chats; try again later"}, origin,
                      {"Retry-After": str(retry_after)})

    found = await _load_demo_agent()
    if found is None:
        return _reply(404, {"detail": "demo not available"}, origin)
    tenant, agent = found
    try:
        request.app.state.agent_assist_throttle.check_mint(str(tenant["id"]))
    except HTTPException as exc:
        return _throttled(exc, origin)

    minted = await agent_testing.mint_test_session(cache.get_client(), tenant=tenant, agent=agent, channel="chat")
    log.info("public demo: chat opened for %s/%s", tenant["slug"], agent["slug"])
    return _reply(200, {"credential": minted["credential"], "session_id": minted["session_id"],
                        "greeting": minted["greeting"], "expires_in": minted["expires_in"],
                        "max_turns": PUBLIC_CHAT_TURNS}, origin)


@router.post("/demo-chat/turn")
async def demo_chat_turn(request: Request):
    origin = request.headers.get("origin", "")
    if origin not in _allowed_origins():
        return JSONResponse(status_code=403, content={"detail": "origin not allowed"})
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_CHAT_BODY:
        return _reply(413, {"detail": "message too long"}, origin)
    try:
        body = PublicChatTurn.model_validate_json(await request.body())
    except ValidationError:
        return _reply(400, {"detail": "invalid message"}, origin)

    found = await _load_demo_agent()
    if found is None:
        return _reply(404, {"detail": "demo not available"}, origin)
    tenant, agent = found
    try:
        request.app.state.agent_assist_throttle.check_turn(str(tenant["id"]))
    except HTTPException as exc:
        return _throttled(exc, origin)

    try:
        reply = await agent_testing.run_chat_turn(
            cache.get_client(), tenant=tenant, agent=agent, session_id=body.session_id,
            credential=body.credential, message=body.message, secret_resolver=_secret_resolver,
            turn_cap=PUBLIC_CHAT_TURNS,
        )
    except agent_testing.TurnLimitReached:
        return _reply(429, {"detail": "turn limit reached"}, origin)
    except LookupError:
        return _reply(404, {"detail": "chat session expired"}, origin)
    except ValueError:
        return _reply(502, {"detail": "ai_unavailable"}, origin)
    return _reply(200, {"reply": reply}, origin)
