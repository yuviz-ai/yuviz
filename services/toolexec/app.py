"""
Tool Execution Service — FastAPI app. Auth reuses services.config deps; JWT_SECRET must match Config Service.

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
    # Fail at startup on a broken POSTGRES_DSN or HMAC key ref, not on the first request.
    await db.get_pool()
    await executor._get_hmac_key()
    yield
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Tool Execution Service", lifespan=lifespan)

# Admin UI is the only browser client.
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
    # Never str(exc): secret resolvers raise KeyError with the ref and mount path in the message.
    log.info("toolexec: %s %s -> 404: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=404, content={"detail": "not found"})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    # str(exc) is intentional: ValueErrors here are client-facing codes; never embed infra details in them.
    log.info("toolexec: %s %s -> 400: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(request: Request, exc: asyncpg.ForeignKeyViolationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "request references an id that does not exist"})


@app.get("/health")
async def health():
    return {"status": "ok"}
