from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from libs.tenancy import set_target_tenant

from .. import call_flows as call_flows_service
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    get_or_404,
    is_platform_scoped,
    require_path_tenant_access,
    require_role,
)
from ..schemas import CallFlowCreate, CallFlowDraft, CallFlowPublish, CallFlowUpdate

# Tier 2 (a tenant path segment) and Tier 3 (a flat by-id route) — see
# docs/rls-tenant-isolation.md. The two shapes exist for the same reason
# every other resource here has both: the list/create pair is naturally
# tenant-scoped, while the editor holds a flow id and nothing else.
tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_slug}/call-flows",
    tags=["call_flows"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/call-flows", tags=["call_flows"])

_CALL_FLOWS_SLUG_UNIQUE = "call_flows_tenant_id_slug_key"


async def _resolve_tenant(tenant_slug: str, current_user: CurrentUser) -> dict:
    """Load tenant by slug; 404 for missing *or* wrong-tenant JWT (lesson 2)."""
    tenant = await get_or_404(
        tenants_service.get_tenant(tenant_slug), f"tenant {tenant_slug!r} not found",
    )
    is_unscoped = current_user.role == "superadmin" or current_user.is_service_account
    if not is_unscoped and str(tenant["id"]) != str(current_user.tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {tenant_slug!r} not found")
    return tenant


def _parse_id(call_flow_id: str) -> str:
    """Reject non-UUID path params before asyncpg raises an unhandled DataError."""
    try:
        uuid.UUID(call_flow_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{call_flow_id!r} is not a valid call flow id")
    return call_flow_id


async def _authorize_flow(call_flow_id: str, current_user: CurrentUser) -> dict:
    """Tier 3: fetch, assert access, then pin the RLS target to the row's own
    tenant so every following statement in this request is scoped to it."""
    _parse_id(call_flow_id)
    platform_scoped = is_platform_scoped(current_user)
    flow = await get_or_404(
        call_flows_service.get_call_flow(call_flow_id, platform_scoped=platform_scoped),
        f"call_flow {call_flow_id!r} not found",
    )
    await assert_tenant_access(flow["tenant_id"], current_user)
    set_target_tenant(flow["tenant_id"])
    return flow


def _validation_error(exc: call_flows_service.CallFlowValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": "call flow is not valid", "errors": [e.to_dict() for e in exc.errors]},
    )


@tenant_scoped_router.get("")
async def list_call_flows(tenant_slug: str, current_user: CurrentUser = Depends(get_current_user)):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    return await call_flows_service.list_call_flows(tenant["id"])


@tenant_scoped_router.get("/{call_flow_id}/published")
async def get_published_call_flow(
    tenant_slug: str, call_flow_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """The runtime read for the conversation service (IConfigProvider.
    get_call_flow()) — deliberately NOT _authorize_flow(), which resolves
    the row first and pins RLS to the row's own tenant. Here {tenant_slug}
    is the DID-resolved tenant and IS the RLS target (bind_path_tenant, on
    the router above) before the row is ever looked up, so a call_flow_id
    naming another tenant's row is invisible rather than rejected. Every
    negative case is the same bare 404 (lesson 2)."""
    _parse_id(call_flow_id)
    payload = await call_flows_service.get_published_for_runtime(tenant_slug, call_flow_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"call_flow {call_flow_id!r} not found")
    return payload


@tenant_scoped_router.post("", status_code=201)
async def create_call_flow(
    tenant_slug: str,
    body: CallFlowCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    tenant = await _resolve_tenant(tenant_slug, current_user)
    if body.clone_from_id is not None:
        # Authorize the clone SOURCE the way any other by-id read is
        # authorized, before it is copied — otherwise "clone" would be a way
        # to read a flow the caller cannot open.
        source = await _authorize_flow(body.clone_from_id, current_user)
        # Cloning across tenants would copy one tenant's flow into another,
        # so it is refused outright rather than left to RLS to fail opaquely.
        if str(source["tenant_id"]) != str(tenant["id"]):
            raise HTTPException(
                status_code=400, detail="a call flow can only be cloned within its own account",
            )
        # _authorize_flow pinned the RLS target to the source flow; the
        # INSERT below belongs to the path tenant, so restore it.
        set_target_tenant(tenant["id"])
    try:
        return await call_flows_service.create_call_flow(
            tenant_id=tenant["id"], slug=body.slug, name=body.name,
            description=body.description, direction=body.direction,
            clone_from_id=body.clone_from_id, graph=body.graph,
            user_id=current_user.id, user_email=current_user.email,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except call_flows_service.CallFlowValidationError as exc:
        return _validation_error(exc)
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == _CALL_FLOWS_SLUG_UNIQUE:
            raise HTTPException(status_code=409, detail=f"call flow {body.slug!r} already exists")
        raise


@router.get("/{call_flow_id}")
async def get_call_flow(call_flow_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await _authorize_flow(call_flow_id, current_user)


@router.patch("/{call_flow_id}")
async def update_call_flow(
    call_flow_id: str,
    body: CallFlowUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_flow(call_flow_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    return await call_flows_service.update_call_flow(
        call_flow_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{call_flow_id}", status_code=204)
async def delete_call_flow(
    call_flow_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_flow(call_flow_id, current_user)
    await call_flows_service.delete_call_flow(
        call_flow_id, user_id=current_user.id, user_email=current_user.email,
    )


@router.post("/{call_flow_id}/validate")
async def validate_call_flow(
    call_flow_id: str,
    body: CallFlowDraft,
    current_user: CurrentUser = Depends(get_current_user),
):
    await _authorize_flow(call_flow_id, current_user)
    try:
        return {"valid": True, "warnings": await call_flows_service.validate(body.graph)}
    except call_flows_service.CallFlowValidationError as exc:
        return _validation_error(exc)


@router.put("/{call_flow_id}/draft")
async def save_draft(
    call_flow_id: str,
    body: CallFlowDraft,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_flow(call_flow_id, current_user)
    try:
        return await call_flows_service.save_draft(
            call_flow_id, body.graph, expected_version=body.expected_version,
        )
    except call_flows_service.StaleDraft as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{call_flow_id}/publish")
async def publish_call_flow(
    call_flow_id: str,
    body: CallFlowPublish,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_flow(call_flow_id, current_user)
    try:
        return await call_flows_service.publish(
            call_flow_id, body.graph, user_id=current_user.id,
            user_email=current_user.email, note=body.note,
        )
    except call_flows_service.CallFlowValidationError as exc:
        return _validation_error(exc)


@router.get("/{call_flow_id}/versions")
async def list_versions(call_flow_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await _authorize_flow(call_flow_id, current_user)
    return await call_flows_service.list_versions(call_flow_id)


@router.post("/{call_flow_id}/versions/{version}/rollback")
async def rollback(
    call_flow_id: str,
    version: int,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """Rollback republishes an old graph as a NEW version rather than moving
    a pointer back — the history stays append-only and a rollback is itself
    auditable."""
    await _authorize_flow(call_flow_id, current_user)
    graph = await call_flows_service.get_version_graph(call_flow_id, version)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"version {version} not found")
    try:
        return await call_flows_service.publish(
            call_flow_id, graph, user_id=current_user.id,
            user_email=current_user.email, note=f"rollback to v{version}",
        )
    except call_flows_service.CallFlowValidationError as exc:
        return _validation_error(exc)
