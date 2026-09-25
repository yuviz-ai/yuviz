"""
Config Service — FastAPI app. A thin HTTP wrapper around tenants.py/agents.py/
provider_configs.py: routers translate HTTP <-> those functions and nothing
else. All business logic (caching, audit, config versioning) already lives
in those modules and in the database triggers — this file has none of its
own.

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
from . import phone_numbers as phone_numbers_service
from . import provider_configs as provider_configs_service
from . import tenants as tenants_service
from .routers import (
    agent_tool_policies, agents, audit_log, auth, call_flows, calls, carriers,
    invites as invites_router,
    live_calls, phone_numbers, provider_configs, telephony_configs, tenants, tool_catalog,
    tool_provider_configs, users,
)

log = logging.getLogger(__name__)


class InviteThrottle:
    """The two counters from the design's "Probe rate limit" section, both
    keyed on the acting admin's JWT subject (`invited_by`) and both checked
    before invites.create_invite's users lookup runs.

    - probe: 30 create attempts/hour, outcome-blind — incremented
      unconditionally by the caller regardless of what create_invite does
      with the request, so a 409 costs exactly what a 201 costs.
    - send: 20 successful sends/hour across create+resend (the mail-bomb
      cap) — only incremented after a create/resend actually succeeds.
    """

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
    """The accept-route IP throttle (design's "Throttle key" section): 10/
    minute and 50/hour, covering GET+POST together, keyed on
    `request.client.host` — **never** `X-Forwarded-For`, which is entirely
    attacker-controlled here (no reverse proxy sits in front of this
    service in deployment/docker/docker-compose.yml; the Admin UI calls
    http://localhost:8000 directly). If a proxy is introduced later, key on
    the right-most untrusted hop of X-Forwarded-For behind an explicit
    trusted_hosts list — never the left-most, and never the header
    unvalidated."""

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
    """Per-user token bucket for GET /live-calls, sized to the 5s poll
    interval (live_calls.py's REFRESH_MS budget), same FixedWindowCounter
    precedent as InviteThrottle/AcceptThrottle above.

    limit=4, not 1: one operator's own legitimate traffic in a single 5s
    window is not always exactly one request. A second browser tab polling
    its own unsynchronized 5s cadence, a superadmin's tenant switch (which
    fires an immediate re-fetch on top of whatever the old interval still
    had in flight), and pause-then-immediate-resume (same — an immediate
    fetch layered on the interval boundary) can all legitimately land 2-3
    requests from the SAME user in one window without any hammering at all.
    limit=1 rejected exactly this traffic (found live via review, not by any
    of this file's own tests — every one of them called _reset_throttle(),
    which is why nothing caught it; see TestRateLimitAndAcquireTimeout's
    dedicated non-reset test for the fix's own proof). 4 gives roughly 3-4x
    the single-tab steady-state rate — enough for 2-3 tabs plus one
    switch/resume on top — while still bounding a client that is actually
    hammering the route to a small constant multiple of its intended cadence,
    not an unbounded one."""

    def __init__(self) -> None:
        self._counter = FixedWindowCounter(limit=4, window_seconds=5)

    def check(self, key: str) -> None:
        over, retry_after = self._counter.over_limit(key)
        if over:
            raise _too_many_requests("too many requests; slow down", retry_after)
        self._counter.increment(key)


def _too_many_requests(detail: str, retry_after: int) -> HTTPException:
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect eagerly, not lazily, so a broken POSTGRES_DSN/REDIS_URL fails at
    # startup — the same "fail fast, not on the first request" reasoning as
    # AIProviderManager.prewarm().
    await db.get_pool()
    cache.get_client()
    # Populate did:{did} for every active DID now — see
    # phone_numbers.py's top-of-file comment for the full design.
    warmed = await phone_numbers_service.prewarm()
    log.info("Prewarmed %d active phone number(s) into Redis", warmed)
    yield
    await db.close_pool()
    await cache.close()
    email.close_smtp_executor()


app = FastAPI(title="Voice AI Platform — Config Service", lifespan=lifespan)

# Admin UI (admin-ui/, Next.js dev server) is the only browser client — this
# is a local-only dev tool, so the origin list stays narrow rather than a
# wildcard. Real request-scoped auth (JWT, see auth.py/deps.py) is enforced
# per-route now, not by CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-process throttle state for the invite routers — see InviteThrottle/
# AcceptThrottle above. Attached to app.state (not module-level singletons
# imported by routers/invites.py) so this file stays the one place that
# constructs them, with no import cycle back from the router it mounts.
app.state.invite_throttle = InviteThrottle()
app.state.accept_throttle = AcceptThrottle()
app.state.live_calls_throttle = LiveCallsThrottle()

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(invites_router.router)
app.include_router(tenants.router)
app.include_router(agents.router)
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
    # A server misconfiguration, not a bad request — but the message says
    # exactly what to set, so it has to reach the caller rather than 500.
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(
    request: Request, exc: asyncpg.ForeignKeyViolationError,
) -> JSONResponse:
    # Defense in depth: routers should validate referenced ids exist before
    # inserting (see provider_configs router's _resolve_tenant_id) — this
    # catches whatever a future route forgets to, so a bad foreign key is
    # always a clean 400, never a raw Postgres constraint name reaching the
    # client.
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


@app.exception_handler(invites.PermissionDenied)
async def _invite_permission_denied(request: Request, exc: invites.PermissionDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "not permitted to invite this role/tenant"})


@app.exception_handler(invites.EmailConflict)
async def _invite_email_conflict(request: Request, exc: invites.EmailConflict) -> JSONResponse:
    # Byte-identical for every tenant_admin case (round-1 CRITICAL, see
    # design doc's "Conflict responses") — tenant_name is None unless the
    # actor was a super_admin, checked by invites._conflict_tenant_name.
    if exc.tenant_name is not None:
        detail = f"email already belongs to tenant '{exc.tenant_name}'"
    else:
        detail = "this email cannot be invited"
    return JSONResponse(status_code=409, content={"detail": detail})


@app.exception_handler(invites.PendingInviteConflict)
async def _invite_pending_conflict(request: Request, exc: invites.PendingInviteConflict) -> JSONResponse:
    # Distinct from EmailConflict (PR #19 finding 1) — no account exists,
    # only a still-live pending invite; the actionable remedy is different
    # so the message is too. Never names a tenant (see the exception's own
    # docstring for why that's safe for both a tenant_admin and superadmin
    # actor).
    return JSONResponse(
        status_code=409,
        content={"detail": "a pending invite already exists for this email; revoke it first"},
    )


@app.exception_handler(invites.InviteNotPending)
async def _invite_not_pending(request: Request, exc: invites.InviteNotPending) -> JSONResponse:
    # Covers resend/revoke of an already-accepted or already-revoked invite.
    # Known gap (see design doc + PR notes): an already-*accepted* invite
    # hits this same branch — revoking it is refused rather than having any
    # effect on the user it already created. Reported, not fixed, here.
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
    # Tenant-blind by construction (see invites.InviteContextGone docstring)
    # — the accepter learns only that the invite is no longer valid.
    return JSONResponse(status_code=410, content={"detail": "invite is no longer valid"})


@app.exception_handler(invites.EmailTaken)
async def _invite_email_taken(request: Request, exc: invites.EmailTaken) -> JSONResponse:
    # Same tenant-blind wording as the create-path conflict — the accepting
    # party is unauthenticated and must learn nothing about the other
    # tenant (design doc's "Step 3's own conflict").
    return JSONResponse(status_code=409, content={"detail": "an account already exists for this email"})


@app.get("/health")
async def health():
    return {"status": "ok"}
