from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import agent_tool_policies as agent_tool_policies_service
from .. import agents as agents_service
from ..auth import CurrentUser
from ..deps import get_current_user, require_role, validate_id_exists
from ..schemas import AgentToolPolicyCreate, AgentToolPolicyUpdate

router = APIRouter(prefix="/agents/{agent_id}/tool-policies", tags=["agent_tool_policies"])


async def _resolve_agent_id(agent_id: str) -> None:
    await validate_id_exists(agent_id, agents_service.get_agent_by_id, "agent")


@router.get("")
async def list_agent_tool_policies(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await agent_tool_policies_service.list_for_agent(agent_id)


@router.post("", status_code=201)
async def create_agent_tool_policy(
    agent_id: str,
    body: AgentToolPolicyCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_agent_id(agent_id)
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
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
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
    try:
        await agent_tool_policies_service.delete_agent_tool_policy(
            agent_id, tool_name, user_id=current_user.id, user_email=current_user.email,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"tool policy {tool_name!r} not found for agent {agent_id!r}")
