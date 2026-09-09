from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import agents as agents_service
from .. import tenants as tenants_service
from .. import workflows as workflows_service
from ..auth import CurrentUser
from ..deps import get_current_user, get_or_404, require_role
from ..schemas import AgentCreate, AgentUpdate, WorkflowDraft, WorkflowPublish

router = APIRouter(prefix="/tenants/{tenant_slug}/agents", tags=["agents"])

# Postgres name for UNIQUE (tenant_id, slug) on agents — don't map other
# unique violations (e.g. agent_workflow_versions) to the slug message.
_AGENTS_SLUG_UNIQUE = "agents_tenant_id_slug_key"


async def _resolve_tenant(tenant_slug: str, current_user: CurrentUser) -> dict:
    """Load tenant by slug; 404 for missing *or* wrong-tenant JWT (lesson 2)."""
    tenant = await get_or_404(
        tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found",
    )
    is_unscoped = current_user.role == "superadmin" or current_user.is_service_account
    if not is_unscoped and str(tenant["id"]) != str(current_user.tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {tenant_slug!r} not found")
    return tenant


def _parse_agent_id(agent_id: str) -> str:
    """Reject non-UUID path params before asyncpg raises an unhandled DataError."""
    try:
        uuid.UUID(agent_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{agent_id!r} is not a valid agent id")
    return agent_id


def _validation_error(exc: workflows_service.WorkflowValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "detail": "workflow is not valid",
            "errors": [e.to_dict() for e in exc.errors],
        },
    )


@router.get("")
async def list_agents(tenant_slug: str, current_user: CurrentUser = Depends(get_current_user)):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    return await agents_service.list_agents(tenant["id"])


@router.post("", status_code=201)
async def create_agent(
    tenant_slug: str,
    body: AgentCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    try:
        return await agents_service.create_agent(
            tenant_id=tenant["id"],
            slug=body.slug,
            name=body.name,
            greeting=body.greeting,
            system_prompt=body.system_prompt,
            stt_config_id=body.stt_config_id,
            llm_config_id=body.llm_config_id,
            tts_config_id=body.tts_config_id,
            workflow=body.workflow,
            tenant_slug=tenant_slug,
            user_id=current_user.id,
            user_email=current_user.email,
        )
    except workflows_service.WorkflowValidationError as exc:
        return _validation_error(exc)
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name != _AGENTS_SLUG_UNIQUE:
            raise
        raise HTTPException(
            status_code=409,
            detail="That name or slug is already taken in this account.",
        )


@router.get("/{agent_slug}")
async def get_agent(tenant_slug: str, agent_slug: str, current_user: CurrentUser = Depends(get_current_user)):
    await _resolve_tenant(tenant_slug, current_user)
    return await get_or_404(
        agents_service.get_agent(tenant_slug, agent_slug),
        f"agent {agent_slug!r} not found under tenant {tenant_slug!r}",
    )


@router.patch("/{agent_id}")
async def update_agent(
    tenant_slug: str,
    agent_id: str,
    body: AgentUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant(tenant_slug, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await agents_service.update_agent(
        agent_id, tenant_slug=tenant_slug,
        user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    tenant_slug: str,
    agent_id: str,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant(tenant_slug, current_user)
    await agents_service.soft_delete_agent(
        agent_id, tenant_slug=tenant_slug, user_id=current_user.id, user_email=current_user.email,
    )


@router.get("/{agent_id}/workflow")
async def get_workflow(
    tenant_slug: str, agent_id: str, current_user: CurrentUser = Depends(get_current_user),
):
    await _resolve_tenant(tenant_slug, current_user)
    agent_id = _parse_agent_id(agent_id)
    return await workflows_service.get_workflow(agent_id, tenant_slug)


@router.put("/{agent_id}/workflow/draft")
async def save_workflow_draft(
    tenant_slug: str,
    agent_id: str,
    body: WorkflowDraft,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant(tenant_slug, current_user)
    agent_id = _parse_agent_id(agent_id)
    try:
        return await workflows_service.save_draft(
            agent_id,
            tenant_slug=tenant_slug,
            graph=body.graph,
            base_config_version=body.base_config_version,
        )
    except workflows_service.StaleDraft:
        raise HTTPException(
            status_code=409,
            detail="workflow draft is stale — reload and try again",
        )


@router.post("/{agent_id}/workflow/validate")
async def validate_workflow(
    tenant_slug: str,
    agent_id: str,
    body: WorkflowDraft,
    current_user: CurrentUser = Depends(get_current_user),
):
    await _resolve_tenant(tenant_slug, current_user)
    _parse_agent_id(agent_id)
    try:
        return {"valid": True, "warnings": await workflows_service.validate(body.graph)}
    except workflows_service.WorkflowValidationError as exc:
        return {"valid": False, "errors": [e.to_dict() for e in exc.errors], "warnings": []}


@router.post("/{agent_id}/workflow/publish")
async def publish_workflow(
    tenant_slug: str,
    agent_id: str,
    body: WorkflowPublish,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant(tenant_slug, current_user)
    agent_id = _parse_agent_id(agent_id)
    try:
        return await workflows_service.publish(
            agent_id, tenant_slug=tenant_slug, graph=body.graph, note=body.note,
            user_id=current_user.id, user_email=current_user.email,
        )
    except workflows_service.WorkflowValidationError as exc:
        return _validation_error(exc)


@router.get("/{agent_id}/workflow/versions")
async def list_workflow_versions(
    tenant_slug: str, agent_id: str, limit: int = 50,
    current_user: CurrentUser = Depends(get_current_user),
):
    await _resolve_tenant(tenant_slug, current_user)
    agent_id = _parse_agent_id(agent_id)
    return await workflows_service.list_versions(agent_id, tenant_slug, limit=limit)


@router.get("/{agent_id}/workflow/versions/{version}")
async def get_workflow_version(
    tenant_slug: str, agent_id: str, version: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    await _resolve_tenant(tenant_slug, current_user)
    agent_id = _parse_agent_id(agent_id)
    return await get_or_404(
        workflows_service.get_version(agent_id, tenant_slug, version),
        f"workflow version {version} not found",
    )


@router.post("/{agent_id}/workflow/versions/{version}/rollback")
async def rollback_workflow(
    tenant_slug: str,
    agent_id: str,
    version: int,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant(tenant_slug, current_user)
    agent_id = _parse_agent_id(agent_id)
    try:
        return await workflows_service.rollback(
            agent_id, tenant_slug=tenant_slug, version=version,
            user_id=current_user.id, user_email=current_user.email,
        )
    except workflows_service.WorkflowValidationError as exc:
        return _validation_error(exc)
