"""Internal endpoints for libs.knowledge_sdk (any authenticated identity); 404 means "no context".
The tenant comes from the body/path, so each handler must assert_tenant_access before set_target_tenant."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant, tenant_conn
from services.config.auth import CurrentUser
from services.config.deps import assert_tenant_access, get_current_user

from .. import agent_kb as agent_kb_service
from .. import db
from .. import retrieval as retrieval_service
from ..runtime import get_embedding_manager, get_vector_repo
from ..schemas import RetrieveRequest

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/agents/{tenant_slug}/{agent_slug}/has-knowledge")
async def has_knowledge(
    tenant_slug: str, agent_slug: str, current_user: CurrentUser = Depends(get_current_user),
):
    await assert_tenant_access(tenant_slug, current_user)
    set_target_tenant(tenant_slug)
    enabled = await agent_kb_service.has_enabled_kb(tenant_slug, agent_slug)
    return {"enabled": enabled}


@router.post("/retrieve")
async def retrieve(
    body: RetrieveRequest, current_user: CurrentUser = Depends(get_current_user),
):
    await assert_tenant_access(body.tenant_slug, current_user)
    set_target_tenant(body.tenant_slug)
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        result = await retrieval_service.retrieve(
            conn,
            await get_vector_repo(),
            get_embedding_manager(),
            tenant_slug=body.tenant_slug,
            agent_slug=body.agent_slug,
            query=body.query,
            top_k=body.top_k,
            max_tokens=body.max_tokens,
            minimum_score=body.minimum_score,
            rerank=body.rerank,
            hybrid_search=body.hybrid_search,
            include_citations=body.include_citations,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="no eligible context for this agent/query")
    return result
