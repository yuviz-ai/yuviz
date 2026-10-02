"""DID Service — FastAPI app. Auth reuses services.config deps; JWT_SECRET must match Config Service.

Run: uvicorn services.did.app:app --reload --port 8200
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .routers import numbers

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail at startup on a broken POSTGRES_DSN, not on the first request.
    await db.get_pool()
    yield
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — DID Service", lifespan=lifespan)

# Admin UI is the only browser client.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(numbers.tenant_scoped_router)
app.include_router(numbers.router)


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(request: Request, exc: asyncpg.ForeignKeyViolationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "request references an id that does not exist"})


@app.get("/health")
async def health():
    return {"status": "ok"}
