from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import (
    bind_path_tenant,
    get_current_user,
    is_platform_scoped,
    require_path_tenant_access,
    require_role,
)

from .. import agent_kb as agent_kb_service
from .. import knowledge_bases as kb_service
from ..schemas import KnowledgeBaseCreate, KnowledgeBaseUpdate

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/knowledge-bases",
    tags=["knowledge_bases"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/knowledge-bases", tags=["knowledge_bases"])


@tenant_scoped_router.get("")
async def list_knowledge_bases(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await kb_service.list_knowledge_bases(tenant_id)


@tenant_scoped_router.post("", status_code=201)
async def create_knowledge_base(
    tenant_id: str,
    body: KnowledgeBaseCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    return await kb_service.create_knowledge_base(
        tenant_id=tenant_id,
        slug=body.slug,
        name=body.name,
        description=body.description,
        embedding_config_id=body.embedding_config_id,
        user_id=current_user.id,
        user_email=current_user.email,
    )


async def _authorize_kb(kb_id: str, current_user: CurrentUser) -> dict:
    """404 — never 403 — both when the knowledge_base doesn't exist and when
    it exists but belongs to a different tenant: a {kb_id} route must not
    become an existence oracle for another tenant's kb_id (lesson 2), which
    is why this keeps its own comparator rather than the shared
    assert_tenant_access (that predicate's UUID branch is 403, correct for
    a *tenant_id* path segment, wrong here). The resolver takes
    platform_scoped so a platform actor's fetch runs under platform_conn
    instead of failing closed under tenant_conn's ambient (unset) scope."""
    not_found = HTTPException(status_code=404, detail=f"knowledge_base {kb_id!r} not found")
    platform_scoped = is_platform_scoped(current_user)
    try:
        kb = await kb_service.get_knowledge_base(kb_id, platform_scoped=platform_scoped)
    except asyncpg.DataError:
        # A malformed non-UUID kb_id reaches asyncpg's uuid column binding —
        # same 404 as a well-formed but nonexistent id (lesson 2).
        raise not_found
    if kb is None or (not platform_scoped and str(kb["tenant_id"]) != current_user.tenant_id):
        raise not_found
    set_target_tenant(str(kb["tenant_id"]))
    return kb


@router.get("/{kb_id}")
async def get_knowledge_base(kb_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await _authorize_kb(kb_id, current_user)


@router.get("/{kb_id}/agents")
async def list_knowledge_base_agents(
    kb_id: str, current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    await _authorize_kb(kb_id, current_user)
    return await agent_kb_service.list_for_kb(kb_id, platform_scoped=is_platform_scoped(current_user))


@router.patch("/{kb_id}")
async def update_knowledge_base(
    kb_id: str,
    body: KnowledgeBaseUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    kb = await _authorize_kb(kb_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    platform_scoped = is_platform_scoped(current_user)
    return await kb_service.update_knowledge_base(
        kb_id,
        platform_scoped=platform_scoped,
        stamp_tenant=str(kb["tenant_id"]) if platform_scoped else None,
        user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    kb = await _authorize_kb(kb_id, current_user)
    platform_scoped = is_platform_scoped(current_user)
    await kb_service.soft_delete_knowledge_base(
        kb_id,
        platform_scoped=platform_scoped,
        stamp_tenant=str(kb["tenant_id"]) if platform_scoped else None,
        user_id=current_user.id, user_email=current_user.email,
    )
