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
import time
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from libs.config_sdk.secrets import SecretEncryptionUnavailable

from . import cache, db, email, invites
from . import phone_numbers as phone_numbers_service
from .routers import (
    agent_tool_policies, agents, audit_log, auth, calls, carriers, invites as invites_router,
    live_calls, phone_numbers, provider_configs, telephony_configs, tenants, tool_catalog,
    tool_provider_configs, users,
)

log = logging.getLogger(__name__)


class FixedWindowCounter:
    """Per-process, per-worker fixed-window rate counter — no Redis, resets
    on restart, and a multi-replica deployment multiplies every limit by
    the replica count (accepted: Config Service runs single-replica today,
    see design doc's throttle risk). `over_limit` only peeks; callers that
    need outcome-blind counting (the invite probe cap) call `increment`
    unconditionally themselves rather than relying on this class to do it
    for them on every check.

    `_buckets` entries are swept eventually, not just reset in place —
    without that, a key that's been reset in place but never deleted is a
    permanent dict entry, and AcceptThrottle keys on the client IP of two
    *public, unauthenticated* routes, so every distinct source address that
    ever hits them would otherwise leak one entry forever.

    Sweeping was originally a full O(n) scan of `_buckets` on *every*
    access — AcceptThrottle's `check()` alone calls into this four times a
    request (two `over_limit` + two `increment`), so with `hour`'s
    3600s window an attacker driving enough distinct source IPs turns the
    rate limiter itself into the bottleneck: it is bounded by "distinct
    IPs per hour", which is the attacker's own free variable, not by
    anything this process controls. Two changes fix that:

    - Below `_SWEEP_THRESHOLD` entries the scan is cheap, so it still runs
      on every access (unchanged behavior at ordinary traffic volumes).
      Above it, the sweep only runs every `_SWEEP_INTERVAL` accesses —
      amortizing the O(n) cost instead of paying it on every request once
      the map is already large. `_accesses_since_sweep` really does count
      accesses, not just mutations: `over_limit` (a non-mutating peek)
      calls `_maybe_sweep` too, and both `over_limit`/`increment` call it
      *before* the capacity check below, every time, regardless of whether
      that check ends up short-circuiting the rest of the call — sweeping
      must never be reachable only through the branch it exists to keep
      unstuck. (Earlier draft called `_maybe_sweep` from inside `_current`,
      which the capacity check `return`ed before reaching — once the map
      hit `_MAX_BUCKETS` nothing ever swept again, so a flood followed by a
      quiet period never recovered: every new key was refused forever
      instead of just until the next sweep evicted the stale ones.) This
      alone does not bound worst-case size: a flood of distinct keys
      arriving within a single window has nothing stale for the sweep to
      remove, no matter how often it runs.
    - `_MAX_BUCKETS` is a hard, explicit cap on distinct keys tracked at
      once. A key not already in `_buckets` is refused once the map is at
      capacity — fails closed (treated as already over limit / not
      recorded) rather than growing past the cap and letting the map,
      and the O(n) sweep cost with it, become unbounded. A capacity
      refusal always forces one extra `_evict_stale` and re-checks before
      actually refusing (`_refuse_new_key`) — without that, a flood that
      fills the map to capacity and then stops leaves every bucket stale
      but the periodic `_SWEEP_INTERVAL`-accesses sweep might not fire for
      another ~500 accesses, so ~499 legitimate requests on brand-new IPs
      would still get refused after the flood is long over. A request
      about to be denied is exactly the moment worth paying the O(n) scan
      for — it happens only when the map is actually full, not on every
      access."""

    _SWEEP_THRESHOLD = 1_000
    _SWEEP_INTERVAL = 500
    _MAX_BUCKETS = 20_000

    def __init__(self, *, limit: int, window_seconds: float):
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}
        self._accesses_since_sweep = 0

    def _evict_stale(self, now: float) -> None:
        stale = [k for k, (window_start, _) in self._buckets.items() if now - window_start >= self._window]
        for k in stale:
            del self._buckets[k]

    def _maybe_sweep(self, now: float) -> None:
        if len(self._buckets) <= self._SWEEP_THRESHOLD:
            self._accesses_since_sweep = 0
            self._evict_stale(now)
            return
        self._accesses_since_sweep += 1
        if self._accesses_since_sweep >= self._SWEEP_INTERVAL:
            self._accesses_since_sweep = 0
            self._evict_stale(now)

    def _at_capacity_for_new_key(self, key: str) -> bool:
        return key not in self._buckets and len(self._buckets) >= self._MAX_BUCKETS

    def _refuse_new_key(self, key: str, now: float) -> bool:
        """True means: don't admit this key. Forces one extra sweep before
        actually refusing — see the class docstring's `_MAX_BUCKETS`
        paragraph for why a denial is the one moment worth the O(n) cost
        regardless of `_SWEEP_INTERVAL`."""
        if not self._at_capacity_for_new_key(key):
            return False
        self._accesses_since_sweep = 0
        self._evict_stale(now)
        return self._at_capacity_for_new_key(key)

    def _current(self, key: str, now: float) -> tuple[float, int]:
        window_start, count = self._buckets.get(key, (now, 0))
        if now - window_start >= self._window:
            window_start, count = now, 0
        return window_start, count

    def over_limit(self, key: str) -> tuple[bool, int]:
        """Returns (over, retry_after_seconds). Does not increment."""
        now = time.monotonic()
        self._maybe_sweep(now)
        if self._refuse_new_key(key, now):
            return True, int(self._window)
        window_start, count = self._current(key, now)
        if count >= self._limit:
            return True, int(self._window - (now - window_start)) + 1
        return False, 0

    def increment(self, key: str) -> None:
        now = time.monotonic()
        self._maybe_sweep(now)
        if self._refuse_new_key(key, now):
            return
        window_start, count = self._current(key, now)
        self._buckets[key] = (window_start, count + 1)


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
    precedent as InviteThrottle/AcceptThrottle above. One request per window
    is exactly one poll tick; a client hammering the route faster than the
    UI's own cadence gets 429 instead of adding load a single-connection
    poll wasn't sized for."""

    def __init__(self) -> None:
        self._counter = FixedWindowCounter(limit=1, window_seconds=5)

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
