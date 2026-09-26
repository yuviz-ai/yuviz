"""
Campaign Service — FastAPI app + the CampaignWorker background task. Same
"routers translate, business logic lives in the modules" convention as
services/config/app.py, services/knowledge/app.py, services/did/app.py.

Auth: imports services.config.auth/deps directly, same temporary choice
services/knowledge/app.py and services/did/app.py already made.

Run: uvicorn services.campaigns.app:app --reload --port 8400
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.config.deps import get_authenticated_user

from . import db
from .routers import campaigns
from .worker import CampaignWorker

# Without this, log.info() calls in originate.py/worker.py are silently
# dropped: uvicorn only configures its own uvicorn.* loggers, so the root
# logger stays at its default WARNING level and this module's INFO-level
# origination/job-resolution traces never appear anywhere — confirmed the
# hard way debugging a real outbound call live (2026-07-28).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

log = logging.getLogger(__name__)

_worker = CampaignWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.get_pool()
    _worker.start()
    yield
    await _worker.stop()
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Campaign Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(campaigns.tenant_scoped_router)
app.include_router(campaigns.router)
app.include_router(campaigns.dnc_tenant_router)
app.include_router(campaigns.dnc_router)


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


class VobizCallResolved(BaseModel):
    call_uuid: str
    succeeded: bool
    detail: str = ""
    call_session_id: str | None = None


@app.post("/internal/vobiz-call-resolved")
async def vobiz_call_resolved(
    body: VobizCallResolved,
    user=Depends(get_authenticated_user),
) -> dict:
    """Internal, service-to-service only — requires service-account JWT auth."""
    await _worker.on_call_resolved(
        body.call_uuid, body.succeeded, body.detail, call_session_id=body.call_session_id,
    )
    return {"ok": True}
