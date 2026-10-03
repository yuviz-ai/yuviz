"""Config Service — FastAPI app; routers only translate HTTP to the service modules.

Run: uvicorn services.config.app:app --reload
"""

from __future__ import annotations

import logging
import time  # noqa: F401
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from libs.config_sdk.secrets import SecretEncryptionUnavailable
from libs.ratelimit import FixedWindowCounter

from . import cache, db, email, invites
from . import agents as agents_service
from . import number_sync
from . import phone_numbers as phone_numbers_service
from . import provider_configs as provider_configs_service
from . import telephony_configs as telephony_configs_service
from . import tenants as tenants_service
from .routers import (
    agent_tool_policies, agents, audit_log, auth, call_flows, calls, carriers,
    invites as invites_router,
    live_calls, phone_numbers, provider_configs, telephony_configs, tenants, tool_catalog,
    tool_provider_configs, users,
)

log = logging.getLogger(__name__)


class InviteThrottle:
    """Per-admin invite limits: probe (30 attempts/h, outcome-blind so a 409
    costs the same as a 201) and send (20 successful sends/h, mail-bomb cap)."""

    def __init__(self) -> None:
        self.probe = FixedWindowCounter(limit=30, window_seconds=3600)
        self.send = FixedWindowCounter(limit=20, window_seconds=3600)

    def check_probe(self, actor_id: str) -> None:
        over, retry_after = self.probe.over_limit(actor_id)
        if over:
            raise _too_many_requests("too many invite attempts; try again later", retry_after)
        self.probe.increment(actor_id)

    def check_send_cap(self, actor_id: str) -> None:
        over, retry_after = self.send.over_limit(actor_id)
        if over:
            raise _too_many_requests("too many invites sent this hour; try again later", retry_after)

    def record_send(self, actor_id: str) -> None:
        self.send.increment(actor_id)


class AcceptThrottle:
    """Per-IP throttle (10/min, 50/h) keyed on request.client.host — never
    X-Forwarded-For, which is attacker-controlled with no proxy in front."""

    def __init__(self) -> None:
        self.minute = FixedWindowCounter(limit=10, window_seconds=60)
        self.hour = FixedWindowCounter(limit=50, window_seconds=3600)

    def check(self, client_host: str) -> None:
        over, retry_after = self.minute.over_limit(client_host)
        if not over:
            over, retry_after = self.hour.over_limit(client_host)
        if over:
            raise _too_many_requests("too many attempts; try again later", retry_after)
        self.minute.increment(client_host)
        self.hour.increment(client_host)


class LiveCallsThrottle:
    """Per-user limit for GET /live-calls per 5s poll window. limit=4, not 1:
    extra tabs, tenant switches and resume legitimately add 2-3 requests."""

    def __init__(self) -> None:
        self._counter = FixedWindowCounter(limit=4, window_seconds=5)

    def check(self, key: str) -> None:
        over, retry_after = self._counter.over_limit(key)
        if over:
            raise _too_many_requests("too many requests; slow down", retry_after)
        self._counter.increment(key)


class AgentAssistThrottle:
    """Per-tenant caps on the guided-creation routes, keyed on the tenant id:
    20 test-credential mints/min, 60 chat turns/min, 20 prompt revises/hour.
    Same FixedWindowCounter precedent as InviteThrottle above."""

    def __init__(self) -> None:
        self.mint = FixedWindowCounter(limit=20, window_seconds=60)
        self.turn = FixedWindowCounter(limit=60, window_seconds=60)
        self.revise = FixedWindowCounter(limit=20, window_seconds=3600)

    @staticmethod
    def _check(counter: FixedWindowCounter, tenant_id: str) -> None:
        over, retry_after = counter.over_limit(tenant_id)
        if over:
            raise _too_many_requests("too many requests; try again later", retry_after)
        counter.increment(tenant_id)

    def check_mint(self, tenant_id: str) -> None:
        self._check(self.mint, tenant_id)

    def check_turn(self, tenant_id: str) -> None:
        self._check(self.turn, tenant_id)

    def check_revise(self, tenant_id: str) -> None:
        self._check(self.revise, tenant_id)


def _too_many_requests(detail: str, retry_after: int) -> HTTPException:
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect eagerly so a broken POSTGRES_DSN/REDIS_URL fails at startup.
    await db.get_pool()
    cache.get_client()
    warmed = await phone_numbers_service.prewarm()
    log.info("Prewarmed %d active phone number(s) into Redis", warmed)
    yield
    await db.close_pool()
    await cache.close()
    email.close_smtp_executor()


app = FastAPI(title="Voice AI Platform — Config Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# On app.state so routers needn't import this module (avoids an import cycle).
app.state.invite_throttle = InviteThrottle()
app.state.accept_throttle = AcceptThrottle()
# Unauthenticated signup and code verify/resend: same per-IP limits, own counters.
app.state.register_throttle = AcceptThrottle()
app.state.verify_throttle = AcceptThrottle()
app.state.live_calls_throttle = LiveCallsThrottle()
app.state.agent_assist_throttle = AgentAssistThrottle()

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(invites_router.router)
app.include_router(tenants.router)
app.include_router(agents.router)
app.include_router(agents.catalog_router)
app.include_router(call_flows.tenant_scoped_router)
app.include_router(call_flows.router)
app.include_router(provider_configs.tenant_scoped_router)
app.include_router(provider_configs.router)
app.include_router(phone_numbers.tenant_scoped_router)
app.include_router(phone_numbers.router)
app.include_router(carriers.tenant_scoped_router)
app.include_router(carriers.router)
app.include_router(calls.tenant_scoped_router)
app.include_router(calls.router)
app.include_router(tool_provider_configs.tenant_scoped_router)
app.include_router(tool_provider_configs.router)
app.include_router(agent_tool_policies.router)
app.include_router(tool_catalog.router)
app.include_router(telephony_configs.tenant_scoped_router)
app.include_router(telephony_configs.router)
app.include_router(telephony_configs.providers_router)
app.include_router(audit_log.router)
app.include_router(live_calls.router)


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(SecretEncryptionUnavailable)
async def _secret_encryption_unavailable(_request, exc: SecretEncryptionUnavailable):
    # Misconfiguration, but the message says what to set, so surface it.
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(
    request: Request, exc: asyncpg.ForeignKeyViolationError,
) -> JSONResponse:
    # Backstop for routes that forget to validate ids; never leak constraint names.
    return JSONResponse(
        status_code=400,
        content={"detail": "request references an id that does not exist"},
    )


@app.exception_handler(tenants_service.TenantHasActiveResources)
async def _tenant_has_active_resources(
    request: Request, exc: tenants_service.TenantHasActiveResources,
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "active_agents": exc.active_agents,
            "active_phone_numbers": exc.active_phone_numbers,
        },
    )


@app.exception_handler(provider_configs_service.ProviderConfigInUse)
async def _provider_config_in_use(
    request: Request, exc: provider_configs_service.ProviderConfigInUse,
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "resource_type": exc.resource_type,
            "resource_count": exc.resource_count,
            "resource_names": exc.resource_names,
        },
    )


@app.exception_handler(agents_service.AgentHasLiveCalls)
async def _agent_has_live_calls(request: Request, exc: agents_service.AgentHasLiveCalls) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "live_call_count": exc.live_call_count},
    )


@app.exception_handler(number_sync.NumberNotInAccount)
async def _number_not_in_account(request: Request, exc: number_sync.NumberNotInAccount) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(number_sync.ProviderUnreachable)
async def _provider_unreachable(request: Request, exc: number_sync.ProviderUnreachable) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(telephony_configs_service.NativeConfigExists)
async def _native_config_exists(request: Request, exc: telephony_configs_service.NativeConfigExists) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(telephony_configs_service.TelephonyConfigHasNumbers)
async def _telephony_config_has_numbers(
    request: Request, exc: telephony_configs_service.TelephonyConfigHasNumbers,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "number_count": exc.count})


@app.exception_handler(phone_numbers_service.DidAlreadyAssigned)
async def _did_already_assigned(request: Request, exc: phone_numbers_service.DidAlreadyAssigned) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(invites.PermissionDenied)
async def _invite_permission_denied(request: Request, exc: invites.PermissionDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "not permitted to invite this role/tenant"})


@app.exception_handler(invites.EmailConflict)
async def _invite_email_conflict(request: Request, exc: invites.EmailConflict) -> JSONResponse:
    # Byte-identical for tenant admins; tenant_name is set only for superadmins.
    if exc.tenant_name is not None:
        detail = f"email already belongs to tenant '{exc.tenant_name}'"
    else:
        detail = "this email cannot be invited"
    return JSONResponse(status_code=409, content={"detail": detail})


@app.exception_handler(invites.PendingInviteConflict)
async def _invite_pending_conflict(request: Request, exc: invites.PendingInviteConflict) -> JSONResponse:
    # No account exists, only a pending invite; never names a tenant.
    return JSONResponse(
        status_code=409,
        content={"detail": "a pending invite already exists for this email; revoke it first"},
    )


@app.exception_handler(invites.InviteNotPending)
async def _invite_not_pending(request: Request, exc: invites.InviteNotPending) -> JSONResponse:
    # Resend/revoke of an accepted or revoked invite; revoking an accepted one
    # doesn't affect the created user.
    return JSONResponse(status_code=409, content={"detail": "invite is not pending"})


@app.exception_handler(invites.ResendCooldown)
async def _invite_resend_cooldown(request: Request, exc: invites.ResendCooldown) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"invite was just sent; try again in {exc.remaining_seconds}s"},
        headers={"Retry-After": str(exc.remaining_seconds)},
    )


@app.exception_handler(invites.InviteExpired)
async def _invite_expired(request: Request, exc: invites.InviteExpired) -> JSONResponse:
    return JSONResponse(status_code=410, content={"detail": "invite has expired"})


@app.exception_handler(invites.InviteRevoked)
async def _invite_revoked(request: Request, exc: invites.InviteRevoked) -> JSONResponse:
    return JSONResponse(status_code=410, content={"detail": "invite has been revoked"})


@app.exception_handler(invites.InviteUsed)
async def _invite_used(request: Request, exc: invites.InviteUsed) -> JSONResponse:
    return JSONResponse(status_code=410, content={"detail": "invite has already been used"})


@app.exception_handler(invites.InviteContextGone)
async def _invite_context_gone(request: Request, exc: invites.InviteContextGone) -> JSONResponse:
    # Tenant-blind: the accepter learns only that the invite is invalid.
    return JSONResponse(status_code=410, content={"detail": "invite is no longer valid"})


@app.exception_handler(invites.EmailTaken)
async def _invite_email_taken(request: Request, exc: invites.EmailTaken) -> JSONResponse:
    # Tenant-blind: the accepter is unauthenticated.
    return JSONResponse(status_code=409, content={"detail": "an account already exists for this email"})


@app.get("/health")
async def health():
    return {"status": "ok"}
