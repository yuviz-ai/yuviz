"""
services/toolexec/routers/agent_apis.py — per-agent enablement (T17).

Every route below is gated by `agent_apis._authorize_agent_api()` (T8) —
but not by calling it directly here: `list_for_agent`/`set_enabled`/
`detach` each call it themselves, INSIDE the service function, before any
read or write. That is what makes a tenant-A admin's PUT of tenant B's
custom_api_id 404 before any INSERT/UPDATE is even attempted — the guard
runs first regardless of which of these three thin handlers is entered.

Writes require `require_role("superadmin","admin")`; the list route is a
read, so it only requires `get_current_user` (mirrors
routers/custom_apis.py's read/write split).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from services.config.auth import CurrentUser
from services.config.deps import get_current_user, require_role

from .. import agent_apis as agent_apis_service
from ..schemas import AgentCustomApiEnable

router = APIRouter(prefix="/agents/{agent_id}/custom-apis", tags=["agent_custom_apis"])


@router.get("")
async def list_agent_custom_apis(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await agent_apis_service.list_for_agent(agent_id, current_user=current_user)


@router.put("/{custom_api_id}")
async def enable_agent_custom_api(
    agent_id: str,
    custom_api_id: str,
    body: AgentCustomApiEnable,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    return await agent_apis_service.set_enabled(
        agent_id, custom_api_id, enabled=body.enabled, current_user=current_user,
        user_id=current_user.id, user_email=current_user.email,
    )


@router.delete("/{custom_api_id}", status_code=204)
async def detach_agent_custom_api(
    agent_id: str,
    custom_api_id: str,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await agent_apis_service.detach(
        agent_id, custom_api_id, current_user=current_user,
        user_id=current_user.id, user_email=current_user.email,
    )
