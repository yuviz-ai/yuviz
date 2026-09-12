"""
Tool Execution Service — FastAPI app. Thin HTTP wrapper, same "routers
translate, business logic lives in the modules" convention as
services/config/app.py and services/knowledge/app.py.

Auth-gated routers mounted here (T16-T19): `get_current_user`/
`require_role`/`require_execute_subject`, imported from
services.config.auth/deps directly rather than a shared libs/auth_sdk,
the same explicit choice services/knowledge/app.py already made. Both
services must share the same JWT_SECRET env var for a token minted by
Config Service's /auth/login to validate here.

Run: uvicorn services.toolexec.app:app --reload --port 8600
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import custom_apis, db, executor
from .routers import agent_apis, chain_runs, custom_apis as custom_apis_router, execute

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect eagerly so a broken POSTGRES_DSN fails at startup, not on the
    # first request — same reasoning as services/knowledge/app.py's lifespan.
    await db.get_pool()
    # Resolve TOOLEXEC_ARGS_HMAC_KEY_REF eagerly too, same fail-loud posture
    # as JWT_SECRET: a misconfigured deploy must not pass /health and then
    # fail on the first real chain's side-effect claim. executor._get_hmac_key()
    # caches by ref, so this is the same call executor.py makes on first use —
    # calling it here just moves WHEN that first call happens.
    await executor._get_hmac_key()
    yield
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Tool Execution Service", lifespan=lifespan)

# Admin UI is the only browser client — same narrow local-dev origin list as
# Config Service's and Knowledge Service's app.py.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(custom_apis_router.tenant_scoped_router)
app.include_router(custom_apis_router.router)
app.include_router(agent_apis.router)
app.include_router(chain_runs.router)
app.include_router(execute.router)


@app.exception_handler(custom_apis.DependentApiExists)
async def dependent_api_exists_handler(request: Request, exc: custom_apis.DependentApiExists) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    # A fixed, content-free detail — never str(exc) (finding 1). KeyError
    # is a LookupError subclass, and both secret resolvers raise KeyError
    # with the credential ref and the absolute secret-mount path baked
    # into the message (libs/config_sdk/secret_resolver.py); str(exc) on
    # that would serialize both straight to the client. This also makes
    # every 404 byte-identical to every other regardless of cause, which
    # is a strict tightening of the existing "absent id / wrong tenant
    # must be indistinguishable" rule (lesson 2), not a relaxation of it —
    # the routers' own authorize-helpers already raise a single fixed
    # string each; this collapses even those to one shared constant.
    log.info("toolexec: %s %s -> 404: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=404, content={"detail": "not found"})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    # str(exc) is returned here BY DESIGN for the registry-validation error
    # codes this service raises deliberately for the client (chain_depth_
    # exceeded, invalid_success_template, credential_ref_not_a_reference,
    # ...) — none of those embed an infrastructure fact. The one class that
    # did (resolve_and_validate_endpoint's resolved-IP / DNS-failure detail,
    # finding 2) was fixed at the SOURCE in custom_apis.py, which now logs
    # the hostname/IP server-side and raises a bare invalid_endpoint_url
    # code — so this handler does not need to blanket-suppress every
    # ValueError's message to close that hole.
    log.info("toolexec: %s %s -> 400: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(request: Request, exc: asyncpg.ForeignKeyViolationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "request references an id that does not exist"})


@app.get("/health")
async def health():
    return {"status": "ok"}
