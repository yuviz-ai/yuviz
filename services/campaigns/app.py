"""Campaign Service — FastAPI app + the CampaignWorker background task.

Run: uvicorn services.campaigns.app:app --reload --port 8400
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .billing import NullBillingAdmission
from .dial_request import DialRequestClient
from .routers import campaigns
from .routers.capacity import capacity_router
from .worker import CampaignWorker

# uvicorn only configures uvicorn.* loggers; without this, INFO logs are dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.get_pool()
    redis_client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    app.state.dial_client = DialRequestClient(
        os.environ.get("KAMAILIO_DIAL_URL", "http://127.0.0.1:5071/campaign-dial"),
        os.environ.get("CAMPAIGNS_KAMAILIO_HMAC_SECRET", "").encode(),
        redis_client,
    )
    app.state.billing = NullBillingAdmission()
    worker = CampaignWorker(
        dial_client=app.state.dial_client, redis_client=redis_client, billing=app.state.billing,
        egress_secret=os.environ.get("CAMPAIGNS_EGRESS_HMAC_SECRET", "").encode(),
    )
    worker.start()
    yield
    await worker.stop()
    await app.state.dial_client.close()
    await app.state.billing.aclose()
    await redis_client.aclose()
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Campaign Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("ADMIN_UI_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(campaigns.tenant_scoped_router)
app.include_router(campaigns.router)
app.include_router(campaigns.dnc_tenant_router)
app.include_router(campaigns.dnc_router)
app.include_router(capacity_router)


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def bad_request_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """400 with loc/msg only: the default body echoes the submitted value in `input` and `ctx`."""
    detail = [{"loc": list(err["loc"]), "msg": err["msg"]} for err in exc.errors()]
    return JSONResponse(status_code=400, content={"detail": detail})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(request: Request, exc: asyncpg.ForeignKeyViolationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "request references an id that does not exist"})


@app.get("/health")
async def health():
    return {"status": "ok"}
