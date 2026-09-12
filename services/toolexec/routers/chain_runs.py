"""
services/toolexec/routers/chain_runs.py — the AC 14 chain-history read
(T18), gated entirely by `agent_apis._authorize_chain_runs()`: the tenant
predicate lives in that function's own query, not here, since
api_chain_runs.session_id is unscoped opaque TEXT with no tenant of its
own to check against the path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from services.config.auth import CurrentUser
from services.config.deps import get_current_user

from .. import agent_apis as agent_apis_service

router = APIRouter(prefix="/calls/{session_id}/chain-runs", tags=["chain_runs"])


@router.get("")
async def get_chain_runs(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await agent_apis_service._authorize_chain_runs(session_id, current_user)
