"""Internal chain-execute endpoint for the Conversation Service.

Gated on a named service identity: every service account is platform-scoped, so scope alone isn't enough.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant
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
    """Require an allow-listed service account (humans, even superadmin, get 403)."""
    if not (user.is_service_account and user.email.lower() in _EXECUTE_SUBJECTS):
        raise HTTPException(status_code=403, detail="identity may not execute API chains")
    return user


@router.post("/execute", response_model=ChainExecuteResponse)
async def execute_chain(
    body: ChainExecuteRequest, current_user: CurrentUser = Depends(require_execute_subject),
) -> ChainExecuteResponse:
    if current_user.tenant_id is not None and current_user.tenant_id != body.tenant_id:
        raise HTTPException(status_code=403, detail="identity may not execute API chains")
    # Tenant comes from the body, so there's no path-based bind_path_tenant.
    set_target_tenant(body.tenant_id)
    return await executor.execute_chain(body)
