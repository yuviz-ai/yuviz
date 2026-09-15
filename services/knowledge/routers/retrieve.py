"""
Internal-only endpoints — the two calls libs.knowledge_sdk's repositories
make. Not meant for the Admin UI; any authenticated identity may call them
(same viewer-role service-account pattern Config SDK's HttpConfigRepository
already uses — see scripts/create_service_account.py), not just superadmin/
admin. A 404 here means "no eligible context", which the SDK's
HttpKnowledgeRepository already maps to None — never a 500 for the normal
"this agent has no KB" case.

Tier 4 (RLS design): the tenant arrives in the request body (retrieve) or a
non-/tenants/ path segment (has-knowledge), so neither carries the caller's
own authorization the way a /tenants/{...} router does. Each handler calls
deps.assert_tenant_access on that tenant, then set_target_tenant, immediately
after its auth gate — a tenant-scoped caller is rejected for a foreign slug,
and Conversation's NULL-tenant service account is admitted with the GUC
resolved to the requested tenant.
"""

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
    # vector_repo/embedding_manager are lazy module-level singletons (see
    # runtime.py) — constructed once, reused across requests, matching
    # AIProviderManager's own "instantiate once, reuse" contract.
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
