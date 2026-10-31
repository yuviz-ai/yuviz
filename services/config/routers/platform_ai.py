from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from libs.config_sdk.languages import LANGUAGES

from .. import platform_ai
from .. import tenants as tenants_service
from ..auth import CurrentUser
from ..deps import bind_path_tenant, get_current_user, require_path_tenant_access, require_role, validate_id_exists
from ..provider_configs import public_provider_config, ref_mask_required
from ..secret_resolver import CompositeSecretResolver

settings_router = APIRouter(prefix="/platform/ai-settings", tags=["platform_ai"])
tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/ai-options",
    tags=["platform_ai"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)

Language = Literal[tuple(LANGUAGES)]  # type: ignore[valid-type]
_secret_resolver = CompositeSecretResolver()


class AiSettingsUpdate(BaseModel):
    local_models_enabled: bool


class AiChoice(BaseModel):
    option_id: str = Field(max_length=120)
    voice: str | None = Field(default=None, max_length=80)
    voice_name: str | None = Field(default=None, max_length=120)
    temperature: float | None = None


class AiRecommendRequest(BaseModel):
    languages: list[Language] = Field(min_length=1, max_length=2)  # type: ignore[valid-type]
    roles: list[Literal["stt", "llm", "tts"]] = Field(default_factory=lambda: ["stt", "llm", "tts"])


@settings_router.get("")
async def get_ai_settings(current_user: CurrentUser = Depends(require_role("superadmin"))):
    """The switch, plus which platform keys are set (names only, never values)."""
    keys: dict[str, dict] = {}
    for o in platform_ai.OPTIONS.values():
        if o.key_env:
            keys.setdefault(o.key_env, {"key_env": o.key_env, "providers": [], "configured": platform_ai.key_configured(o)})
            if o.provider not in keys[o.key_env]["providers"]:
                keys[o.key_env]["providers"].append(o.provider)
    return {**await platform_ai.get_settings(), "keys": list(keys.values())}


@settings_router.put("")
async def put_ai_settings(body: AiSettingsUpdate, current_user: CurrentUser = Depends(require_role("superadmin"))):
    return await platform_ai.set_settings(local_models_enabled=body.local_models_enabled, user_id=current_user.id)


@tenant_scoped_router.get("")
async def list_ai_options(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    settings = await platform_ai.get_settings()
    options = platform_ai.catalog(settings["local_models_enabled"])
    if current_user.role == "superadmin":
        for o in options:
            o["key_env"] = platform_ai.OPTIONS[o["id"]].key_env
    return {
        "local_models_enabled": settings["local_models_enabled"],
        "platform_fee_per_min": platform_ai.PLATFORM_FEE_PER_MIN,
        "options": options,
    }


@tenant_scoped_router.get("/voices")
async def list_option_voices(
    tenant_id: str, option_id: str = Query(max_length=120), current_user: CurrentUser = Depends(get_current_user),
):
    return await platform_ai.option_voices(option_id)


@tenant_scoped_router.get("/preview")
async def preview_option_voice(
    tenant_id: str,
    option_id: str = Query(max_length=120),
    voice: str = Query(max_length=80),
    language: Language = Query(default="en"),  # type: ignore[valid-type]
    current_user: CurrentUser = Depends(get_current_user),
):
    wav = await platform_ai.option_preview(option_id, voice, language, secret_resolver=_secret_resolver)
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


@tenant_scoped_router.post("/choose", tags=["provider_configs"])
async def choose_ai_option(
    tenant_id: str, body: AiChoice, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")
    row = await platform_ai.choose(
        tenant_id, body.option_id, voice=body.voice, voice_name=body.voice_name, temperature=body.temperature,
        user_id=current_user.id, user_email=current_user.email,
    )
    return public_provider_config(row, masked=ref_mask_required(current_user))


@tenant_scoped_router.post("/recommended", tags=["provider_configs"])
async def recommended_ai_options(
    tenant_id: str, body: AiRecommendRequest, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    """The platform's pick per role for these languages, as workspace rows; a role with nothing available is left out."""
    await validate_id_exists(tenant_id, tenants_service.get_tenant_by_id, "tenant")
    local_enabled = (await platform_ai.get_settings())["local_models_enabled"]
    out = []
    for role in body.roles:
        pick = platform_ai.recommended(role, list(body.languages), local_enabled)
        if pick is not None:
            option, voice = pick
            row = await platform_ai.choose(
                tenant_id, option.id, voice=voice, user_id=current_user.id, user_email=current_user.email,
            )
            out.append(public_provider_config(row, masked=ref_mask_required(current_user)))
    return out
