from __future__ import annotations

import uuid
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from libs.tenancy import tenant_conn

from .. import agent_templates
from .. import agent_testing
from .. import agents as agents_service
from .. import cache, db
from .. import tenants as tenants_service
from .. import workflows as workflows_service
from ..auth import CurrentUser
from ..deps import bind_path_tenant, get_current_user, get_or_404, require_path_tenant_access, require_role
from ..schemas import (
    AgentCreate, AgentFromTemplate, AgentUpdate, PromptAccept, PromptRevise, PromptRewrite,
    SystemPromptGenerate, TestChatTurn, TestSessionCreate, WorkflowDraft, WorkflowPublish,
)
from ..secret_resolver import CompositeSecretResolver
from ..system_prompt import (
    CustomerDataError, PromptStructureError, adds_template_braces, check_prompt_structure,
    enforce_prompt_structure, find_customer_data, generate_system_prompt, revise_system_prompt,
    rewrite_system_prompt,
)

_secret_resolver = CompositeSecretResolver()

router = APIRouter(
    prefix="/tenants/{tenant_slug}/agents",
    tags=["agents"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)

# Outside /tenants/{tenant_slug}/agents so it cannot shadow an agent slug.
catalog_router = APIRouter(prefix="/agent-templates", tags=["agents"])

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


async def _load_agent(tenant: dict, agent_id: str) -> dict:
    """The tenant's own live agent, or 404. A foreign id and a random id answer alike."""
    agent = await agents_service.get_agent_by_id(_parse_agent_id(agent_id))
    if agent is None or str(agent["tenant_id"]) != str(tenant["id"]):
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


def _agent_channel(agent: dict) -> Literal["voice", "chat"]:
    # Channel belongs to the job, not the version, so an agent made from an older version keeps it.
    template = next((t for t in agent_templates.CATALOG if t.id == agent["template_id"]), None)
    return "chat" if template is not None and template.channel == "chat" else "voice"


async def _test_transcript(tenant_slug: str, agent_id: str, session_id: str) -> list:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        transcript = await agent_testing.load_test_transcript(
            conn, tenant_slug=tenant_slug, agent_id=agent_id, session_id=session_id,
        )
    if not transcript:
        raise HTTPException(status_code=404, detail="test session not found")
    return transcript


async def _conflict_or_missing(tenant: dict, agent_id: str, conflict: str) -> None:
    """A conditional update matched no row: 409 when the agent is this tenant's, else 404."""
    await _load_agent(tenant, agent_id)
    raise HTTPException(status_code=409, detail=conflict)


def _raise_slug_conflict(exc: asyncpg.UniqueViolationError) -> None:
    if exc.constraint_name != _AGENTS_SLUG_UNIQUE:
        raise exc
    raise HTTPException(
        status_code=409,
        detail="That name or slug is already taken in this account.",
    )


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


@router.post("/generate-system-prompt")
async def generate_agent_system_prompt(
    tenant_slug: str,
    body: SystemPromptGenerate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Wizard's Review step — one-shot LLM draft from structured inputs.
    Never persists anything; the caller still has to Create the agent."""
    tenant = await _resolve_tenant(tenant_slug, current_user)
    try:
        text = await generate_system_prompt(
            tenant["id"], body.llm_config_id, body.model_dump(exclude={"llm_config_id"}),
            secret_resolver=_secret_resolver,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"system_prompt": text}


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
            language=body.language,
            supported_languages=body.supported_languages,
            tts_config_by_language=body.tts_config_by_language,
            greeting_by_language=body.greeting_by_language,
            status=body.status,
            tenant_slug=tenant_slug,
            user_id=current_user.id,
            user_email=current_user.email,
        )
    except workflows_service.WorkflowValidationError as exc:
        return _validation_error(exc)
    except asyncpg.UniqueViolationError as exc:
        _raise_slug_conflict(exc)


@catalog_router.get("")
async def list_agent_templates(_: CurrentUser = Depends(require_role("superadmin", "admin"))):
    return agent_templates.public_catalog()


@router.post("/from-template", status_code=201)
async def create_agent_from_template(
    tenant_slug: str,
    body: AgentFromTemplate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    template = agent_templates.get_template(body.template_id, body.template_version)
    if template is None:
        raise HTTPException(status_code=400, detail="unknown template")
    config_ids = {"stt": body.stt_config_id, "llm": body.llm_config_id, "tts": body.tts_config_id}
    for role, config_id in config_ids.items():
        if (role in template.needs) != (config_id is not None):
            raise HTTPException(
                status_code=400,
                detail=f"{role}_config_id is required" if role in template.needs
                else f"{role}_config_id is not used by this template",
            )
    slug = agent_templates.slugify(body.name)
    if not slug:
        raise HTTPException(status_code=400, detail="name must contain a letter or digit")
    greeting, system_prompt = agent_templates.render(
        template, name=body.name, business_name=body.business_name, facts=body.business_facts,
    )
    try:
        return await agents_service.create_agent(
            tenant_id=tenant["id"],
            slug=slug,
            name=body.name,
            greeting=greeting,
            system_prompt=system_prompt,
            stt_config_id=body.stt_config_id,
            llm_config_id=body.llm_config_id,
            tts_config_id=body.tts_config_id,
            workflow=None,
            language=body.language,
            status="inactive",
            template_id=template.id,
            template_version=template.version,
            tenant_slug=tenant_slug,
            user_id=current_user.id,
            user_email=current_user.email,
        )
    except asyncpg.UniqueViolationError as exc:
        _raise_slug_conflict(exc)


@router.post("/{agent_id}/test-sessions")
async def create_test_session(
    request: Request,
    tenant_slug: str,
    agent_id: str,
    body: TestSessionCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    agent = await _load_agent(tenant, agent_id)
    if body.channel != _agent_channel(agent):
        raise HTTPException(status_code=400, detail="channel does not match this agent")
    request.app.state.agent_assist_throttle.check_mint(str(tenant["id"]))
    return await agent_testing.mint_test_session(
        cache.get_client(), tenant=tenant, agent=agent, channel=body.channel,
    )


@router.post("/{agent_id}/test-chat")
async def test_chat(
    request: Request,
    tenant_slug: str,
    agent_id: str,
    body: TestChatTurn,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    agent = await _load_agent(tenant, agent_id)
    request.app.state.agent_assist_throttle.check_turn(str(tenant["id"]))
    try:
        reply = await agent_testing.run_chat_turn(
            cache.get_client(), tenant=tenant, agent=agent, session_id=body.session_id,
            credential=body.credential, message=body.message, secret_resolver=_secret_resolver,
        )
    except agent_testing.TurnLimitReached:
        raise HTTPException(status_code=429, detail="turn limit reached")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError:
        raise HTTPException(status_code=502, detail="ai_unavailable")
    return {"reply": reply}


@router.post("/{agent_id}/prompt/revise")
async def revise_prompt(
    request: Request,
    tenant_slug: str,
    agent_id: str,
    body: PromptRevise,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Never persists anything. The checks run in a fixed order so the response
    for one caller does not vary with anything outside the caller's own data."""
    tenant = await _resolve_tenant(tenant_slug, current_user)
    agent = await _load_agent(tenant, agent_id)
    if not check_prompt_structure(agent["system_prompt"]):
        raise HTTPException(status_code=422, detail="prompt_not_fixable")
    request.app.state.agent_assist_throttle.check_revise(str(tenant["id"]))
    transcript = await _test_transcript(tenant_slug, agent_id, body.session_id)
    llm_config_id = body.llm_config_id or agent["llm_config_id"] or tenant["default_llm_config_id"]
    try:
        after = await revise_system_prompt(
            tenant["id"], llm_config_id, base_prompt=agent["system_prompt"], problem=body.problem,
            transcript=transcript, channel=_agent_channel(agent), secret_resolver=_secret_resolver,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CustomerDataError:
        raise HTTPException(status_code=422, detail="customer_data")
    except PromptStructureError:
        raise HTTPException(status_code=422, detail="unusable_output")
    except ValueError:
        raise HTTPException(status_code=502, detail="ai_unavailable")
    return {
        "before": agent["system_prompt"],
        "after": after,
        "base_prompt_sha256": agents_service._sha256_hex(agent["system_prompt"]),
    }


@router.post("/{agent_id}/prompt/rewrite")
async def rewrite_prompt(
    request: Request,
    tenant_slug: str,
    agent_id: str,
    body: PromptRewrite,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Editor's "Ask AI" box. Never persists; the editor saves the result like a hand edit."""
    tenant = await _resolve_tenant(tenant_slug, current_user)
    agent = await _load_agent(tenant, agent_id)
    request.app.state.agent_assist_throttle.check_revise(str(tenant["id"]))
    llm_config_id = agent["llm_config_id"] or tenant["default_llm_config_id"]
    if not llm_config_id:
        raise HTTPException(status_code=400, detail="no_ai_model")
    try:
        after = await rewrite_system_prompt(
            tenant["id"], llm_config_id, base_prompt=body.prompt, instruction=body.instruction,
            secret_resolver=_secret_resolver,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PromptStructureError:
        raise HTTPException(status_code=422, detail="unusable_output")
    except ValueError:
        raise HTTPException(status_code=502, detail="ai_unavailable")
    return {"after": after}


@router.post("/{agent_id}/prompt/accept")
async def accept_prompt(
    tenant_slug: str,
    agent_id: str,
    body: PromptAccept,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    agent = await _load_agent(tenant, agent_id)
    transcript = await _test_transcript(tenant_slug, agent_id, body.session_id)
    proposed = body.proposed_prompt
    try:
        well_formed = enforce_prompt_structure(proposed, channel=_agent_channel(agent)) == proposed
    except PromptStructureError:
        well_formed = False
    caller_lines = [caller for caller, _ in transcript if caller]
    if (
        not well_formed
        or adds_template_braces(proposed, agent["system_prompt"])
        or find_customer_data(
            proposed, base=agent["system_prompt"], problem=body.problem, caller_lines=caller_lines,
        )
    ):
        raise HTTPException(status_code=400, detail="proposed prompt is not acceptable")
    accepted = await agents_service.accept_prompt_revision(
        agent["id"], tenant_id=tenant["id"], tenant_slug=tenant_slug, proposed_prompt=proposed,
        base_prompt_sha256=body.base_prompt_sha256,
        user_id=current_user.id, user_email=current_user.email,
    )
    if accepted is None:
        await _conflict_or_missing(tenant, agent_id, "the instructions changed since this fix was prepared")
    return accepted


@router.post("/{agent_id}/prompt/undo")
async def undo_prompt(
    tenant_slug: str,
    agent_id: str,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    undone = await agents_service.undo_prompt_revision(
        _parse_agent_id(agent_id), tenant_id=tenant["id"], tenant_slug=tenant_slug,
        user_id=current_user.id, user_email=current_user.email,
    )
    if undone is None:
        await _conflict_or_missing(tenant, agent_id, "there is no fix to undo")
    return undone


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
