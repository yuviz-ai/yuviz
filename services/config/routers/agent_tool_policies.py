from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant

from .. import agent_tool_policies as agent_tool_policies_service
from .. import agents as agents_service
from ..auth import CurrentUser
from ..deps import assert_tenant_access, get_current_user, get_or_404, is_platform_scoped, require_role
from ..schemas import AgentToolPolicyCreate, AgentToolPolicyUpdate

router = APIRouter(prefix="/agents/{agent_id}/tool-policies", tags=["agent_tool_policies"])


async def _authorize_agent(agent_id: str, current_user: CurrentUser) -> dict:
    """agent_tool_policies has no tenant_id of its own (Wave B child table,
    RLS-visible only via its parent agents row) — so the tenant to check
    against is the parent agent's, fetched the same way every other Tier 3
    resolver does (platform_scoped from deps.is_platform_scoped, lesson 24)."""
    agent = await get_or_404(
        agents_service.get_agent_by_id(agent_id, platform_scoped=is_platform_scoped(current_user)),
        f"agent {agent_id!r} not found",
    )
    await assert_tenant_access(agent["tenant_id"], current_user)
    return agent


@router.get("")
async def list_agent_tool_policies(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    agent = await _authorize_agent(agent_id, current_user)
    set_target_tenant(agent["tenant_id"])
    return await agent_tool_policies_service.list_for_agent(agent_id)


@router.post("", status_code=201)
async def create_agent_tool_policy(
    agent_id: str,
    body: AgentToolPolicyCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    agent = await _authorize_agent(agent_id, current_user)
    set_target_tenant(agent["tenant_id"])
    try:
        return await agent_tool_policies_service.create_agent_tool_policy(
            agent_id=agent_id,
            tool_name=body.tool_name,
            tool_provider_config_id=body.tool_provider_config_id,
            enabled=body.enabled,
            timeout_ms=body.timeout_ms,
            max_calls_per_turn=body.max_calls_per_turn,
            max_chain_depth=body.max_chain_depth,
            user_id=current_user.id,
            user_email=current_user.email,
        )
    except Exception as e:
        # UNIQUE(agent_id, tool_name) violation — this agent already has a
        # policy for this tool, surfaced as a clean 409 rather than a raw
        # asyncpg constraint error.
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"agent {agent_id!r} already has a policy for tool {body.tool_name!r}")
        raise


@router.patch("/{tool_name}")
async def update_agent_tool_policy(
    agent_id: str,
    tool_name: str,
    body: AgentToolPolicyUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    agent = await _authorize_agent(agent_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    set_target_tenant(agent["tenant_id"])
    try:
        return await agent_tool_policies_service.update_agent_tool_policy(
            agent_id, tool_name, user_id=current_user.id, user_email=current_user.email, **fields,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"tool policy {tool_name!r} not found for agent {agent_id!r}")


@router.delete("/{tool_name}", status_code=204)
async def delete_agent_tool_policy(
    agent_id: str,
    tool_name: str,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    agent = await _authorize_agent(agent_id, current_user)
    set_target_tenant(agent["tenant_id"])
    try:
        await agent_tool_policies_service.delete_agent_tool_policy(
            agent_id, tool_name, user_id=current_user.id, user_email=current_user.email,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"tool policy {tool_name!r} not found for agent {agent_id!r}")
