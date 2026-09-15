from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import assert_tenant_access, get_current_user, is_platform_scoped, require_role

from .. import agent_kb as agent_kb_service
from .. import retrieval_policies as policy_service
from ..schemas import RetrievalPolicyUpdate

router = APIRouter(prefix="/agents/{agent_id}/retrieval-policy", tags=["retrieval_policies"])


async def _authorize_agent(agent_id: str, current_user: CurrentUser) -> None:
    """Found with no tenant check at all (T21/T36's coverage tripwire
    surfaced this router being absent from the design's own Tier 3 table
    entirely) — mirrors agent_kb.py's `_authorize_agent`, the established
    pattern for a route keyed on agent_id with no tenant column of its
    own to check directly."""
    platform_scoped = is_platform_scoped(current_user)
    tenant_id = await agent_kb_service.get_agent_tenant_id(agent_id, platform_scoped=platform_scoped)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")
    await assert_tenant_access(tenant_id, current_user)
    set_target_tenant(tenant_id)


@router.get("")
async def get_retrieval_policy(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await _authorize_agent(agent_id, current_user)
    policy = await policy_service.get_policy(agent_id)
    return policy or {"agent_id": agent_id}


@router.put("")
async def set_retrieval_policy(
    agent_id: str,
    body: RetrievalPolicyUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_agent(agent_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to set")
    return await policy_service.upsert_policy(
        agent_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )
