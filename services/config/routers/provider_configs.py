from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from libs.tenancy import set_target_tenant

from .. import provider_configs as provider_configs_service
from .. import tenants as tenants_service
from .. import voice_preview
from ..auth import CurrentUser
from ..deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    get_or_404,
    is_platform_scoped,
    require_path_tenant_access,
    require_role,
    validate_id_exists,
)
from ..schemas import ProviderConfigCreate, ProviderConfigUpdate, VoicePreview
from ..secret_resolver import CompositeSecretResolver

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/providers",
    tags=["provider_configs"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(prefix="/providers", tags=["provider_configs"])
_secret_resolver = CompositeSecretResolver()


async def _resolve_tenant_id(tenant_id: str) -> None:
    """Raises a clean 400/404 for a malformed or nonexistent tenant_id,
    instead of letting the INSERT's FK constraint violation reach the
    client as an unhandled 500 with a Postgres constraint name in it."""
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")


@tenant_scoped_router.get("")
async def list_provider_configs(
    tenant_id: str,
    role: Literal["stt", "llm", "tts", "embedding"] | None = Query(default=None),
    environment: Literal["prod", "staging", "dev"] | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
):
    # Same scoping the by-id routes get via _authorize_provider, and for the
    # same reason — more so now that api_key_ref can be an enc: ref carrying
    # the credential itself rather than a pointer to one. Without this any
    # authenticated user could list another tenant's provider rows by id.
    await assert_tenant_access(tenant_id, current_user)
    return await provider_configs_service.list_provider_configs(
        tenant_id, role=role, environment=environment,
    )


@tenant_scoped_router.post("", status_code=201)
async def create_provider_config(
    tenant_id: str,
    body: ProviderConfigCreate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _resolve_tenant_id(tenant_id)
    return await provider_configs_service.create_provider_config(
        tenant_id=tenant_id,
        name=body.name,
        role=body.role,
        engine=body.engine,
        environment=body.environment,
        model=body.model,
        voice=body.voice,
        language=body.language,
        region=body.region,
        api_key_ref=body.api_key_ref,
        api_key=body.api_key,
        extra=body.extra,
        user_id=current_user.id,
        user_email=current_user.email,
    )


async def _authorize_provider(provider_id: str, current_user: CurrentUser) -> dict:
    """404 if the provider doesn't exist; 403 if it exists but belongs to a
    different tenant than the caller.

    Exemption predicate is `deps.is_platform_scoped` (tenant_id IS NULL,
    lesson 24) — a real superadmin account can have a non-null tenant_id
    set (a leftover default from account creation, unrelated to their
    actual unrestricted access), so `role == "superadmin"` alone would
    over-grant; and the Conversation Service's own internal service
    account (conversation-service@internal.yuviz.ai, role=viewer,
    tenant_id=NULL) legitimately reads provider configs across every
    tenant it serves calls for, one process handling all tenants — a
    role=="superadmin"-only check broke this and made every live call
    silently fall back to agent_config.py's hardcoded legacy default,
    confirmed live as the "Hello! How can I help you today?" greeting
    instead of the real configured one.

    Without this authorization at all, any authenticated tenant-scoped
    admin/viewer could read, edit, or delete another tenant's
    provider_config by id, and list_provider_voices would resolve *that*
    tenant's real api_key_ref and burn its ElevenLabs quota using their
    key."""
    platform_scoped = is_platform_scoped(current_user)
    cfg = await get_or_404(
        provider_configs_service.get_provider_config(provider_id, platform_scoped=platform_scoped),
        f"provider_config {provider_id!r} not found",
    )
    await assert_tenant_access(cfg["tenant_id"], current_user)
    return cfg


@router.get("/{provider_id}")
async def get_provider_config(provider_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await _authorize_provider(provider_id, current_user)


@router.patch("/{provider_id}")
async def update_provider_config(
    provider_id: str,
    body: ProviderConfigUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_provider(provider_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    # A cleared key fails silently until the next real call to this
    # provider — allowed only when paired with a real replacement in the
    # same request (a rotation, not a clear).
    if "api_key_ref" in fields and not (fields["api_key_ref"] or "").strip() and not (fields.get("api_key") or "").strip():
        raise HTTPException(status_code=400, detail="api_key_ref must not be blank")
    set_target_tenant(cfg["tenant_id"])
    return await provider_configs_service.update_provider_config(
        provider_id, user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.delete("/{provider_id}", status_code=204)
async def delete_provider_config(
    provider_id: str, force: bool = False, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    cfg = await _authorize_provider(provider_id, current_user)
    set_target_tenant(cfg["tenant_id"])
    await provider_configs_service.soft_delete_provider_config(
        provider_id, user_id=current_user.id, user_email=current_user.email, force=force,
    )


@router.get("/{provider_id}/voices")
async def list_provider_voices(provider_id: str, current_user: CurrentUser = Depends(get_current_user)):
    cfg = await _authorize_provider(provider_id, current_user)
    set_target_tenant(cfg["tenant_id"])
    return await provider_configs_service.list_elevenlabs_voices(
        provider_id, secret_resolver=_secret_resolver,
    )


@router.post("/{provider_id}/preview")
async def preview_provider_voice(
    provider_id: str,
    body: VoicePreview,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Speak the caller's own text in this voice. Same Tier 3 authorization
    and same one-place-resolves-the-secret rule as /voices above — the key is
    used for one outbound call and never reaches the admin UI."""
    cfg = await _authorize_provider(provider_id, current_user)
    set_target_tenant(cfg["tenant_id"])
    try:
        wav = await voice_preview.synthesize_preview(
            cfg, body.text, secret_resolver=_secret_resolver,
        )
    except voice_preview.PreviewUnavailable as exc:
        raise HTTPException(status_code=400, detail=exc.detail)
    return Response(
        content=wav,
        media_type="audio/wav",
        # A preview is regenerated whenever the text changes, and the text is
        # in the request body rather than the URL, so caching it would serve
        # the previous line for a changed prompt.
        headers={"Cache-Control": "no-store"},
    )
