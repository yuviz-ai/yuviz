"""
services/toolexec/routers/execute.py — the internal service boundary
(T19). Conversation Service's only call into this service; firing a
side-effecting chain in a tenant is a write, so this is gated on a NAMED
service identity, never `get_current_user` alone and never
`is_platform_scoped` alone: every service account on this platform
(Conversation, vobiz, the Config/Knowledge SDK accounts) is a
`role="viewer"`, `tenant_id=NULL` identity, so "platform-scoped" would let
any one of them fire any tenant's side-effecting API chain (lesson 24's
inverse — scope answers "which tenant", never "may this actor act").
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import get_current_user

from .. import executor
from ..schemas import ChainExecuteRequest, ChainExecuteResponse

router = APIRouter(prefix="/internal/chains", tags=["execute"])

_EXECUTE_SUBJECTS = frozenset(
    e.strip().lower() for e in os.environ.get(
        "TOOLEXEC_EXECUTE_SUBJECTS", "conversation-service@internal.yuviz.ai",
    ).split(",") if e.strip()
)


async def require_execute_subject(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Named-identity gate: the caller must be a service account whose
    email is in the operator-configured allow-list. `is_service_account`
    is carried in the JWT (services/config/auth.py), so this needs no DB
    read and cannot be satisfied by a human console account — a human
    `superadmin` still 403s here, which is exactly what proves the
    `is_service_account` half is load-bearing, not just the email match.

    403 (not 404) is correct: the caller is a platform service, not a
    tenant actor, so there is no tenant boundary to leak existence across."""
    if not (user.is_service_account and user.email.lower() in _EXECUTE_SUBJECTS):
        raise HTTPException(status_code=403, detail="identity may not execute API chains")
    return user


@router.post("/execute", response_model=ChainExecuteResponse)
async def execute_chain(
    body: ChainExecuteRequest, current_user: CurrentUser = Depends(require_execute_subject),
) -> ChainExecuteResponse:
    # A tenant-scoped service account (should one ever exist) must
    # additionally match the body's own tenant_id — checked here, since
    # only the handler has the body.
    if current_user.tenant_id is not None and current_user.tenant_id != body.tenant_id:
        raise HTTPException(status_code=403, detail="identity may not execute API chains")
    return await executor.execute_chain(body)
