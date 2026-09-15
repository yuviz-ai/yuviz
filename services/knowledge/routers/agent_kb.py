from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import assert_tenant_access, get_current_user, is_platform_scoped, require_role

from .. import agent_kb as agent_kb_service
from ..schemas import AgentKnowledgeBaseCreate, AgentKnowledgeBaseUpdate

router = APIRouter(prefix="/agents/{agent_id}/knowledge-bases", tags=["agent_knowledge_bases"])


@router.get("")
async def list_agent_knowledge_bases(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await _authorize_agent(agent_id, current_user)
    return await agent_kb_service.list_for_agent(agent_id)


@router.post("", status_code=201)
async def assign_knowledge_base(
    agent_id: str,
    body: AgentKnowledgeBaseCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_agent(agent_id, current_user)
    return await agent_kb_service.assign(agent_id, body.kb_id, enabled=body.enabled)


async def _authorize_agent(agent_id: str, current_user: CurrentUser) -> None:
    """update_assignment/detach_knowledge_base are keyed on agent_id rather
    than on a row of their own — agent_knowledge_bases carries no tenant
    column — so the check resolves the *agent's* tenant (no check today)."""
    platform_scoped = is_platform_scoped(current_user)
    tenant_id = await agent_kb_service.get_agent_tenant_id(agent_id, platform_scoped=platform_scoped)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")
    await assert_tenant_access(tenant_id, current_user)
    set_target_tenant(tenant_id)


@router.patch("/{kb_id}")
async def update_assignment(
    agent_id: str,
    kb_id: str,
    body: AgentKnowledgeBaseUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_agent(agent_id, current_user)
    try:
        return await agent_kb_service.set_enabled(agent_id, kb_id, enabled=body.enabled)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{kb_id}", status_code=204)
async def detach_knowledge_base(
    agent_id: str, kb_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_agent(agent_id, current_user)
    await agent_kb_service.detach(agent_id, kb_id)
