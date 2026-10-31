"""Voice typing in the console: a short recorded clip transcribed with the agent's own hearing (STT) model.
Cloud engines only (local Whisper runs inside the Conversation service), so a local agent borrows the
platform's recommended cloud model."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from libs.config_sdk.languages import deepgram_supports_multi, normalize_language

from .provider_configs import get_provider_config
from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

MAX_CLIP_BYTES = 2 * 1024 * 1024  # about a minute of browser opus audio
ALLOWED_TYPES = {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav"}
_TIMEOUT_S = 20.0
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


class VoiceInputUnavailable(ValueError):
    """The clip can't be transcribed for a reason the admin can fix; the message says how."""


def _languages(agent: dict[str, Any]) -> list[str]:
    langs = [normalize_language(x) for x in (agent.get("supported_languages") or [])]
    primary = normalize_language(agent.get("language"))
    out = [l for l in [primary, *langs] if l]
    return list(dict.fromkeys(out)) or ["en"]


def _deepgram_host() -> str:
    host = os.environ.get("DEEPGRAM_API_HOST", "").strip().lower()
    return host if host and _HOST_RE.fullmatch(host) else "api.deepgram.com"


async def _deepgram(cfg: dict[str, Any], key: str, audio: bytes, content_type: str, langs: list[str]) -> str:
    model = cfg.get("model") or "nova-3"
    language = "multi" if len(langs) > 1 and deepgram_supports_multi(model, langs) else langs[0]
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            f"https://{_deepgram_host()}/v1/listen",
            params={"model": model, "language": language, "smart_format": "true"},
            headers={"Authorization": f"Token {key}", "Content-Type": content_type},
            content=audio,
        )
    if resp.status_code != 200:
        log.warning("voice input: Deepgram returned %s: %s", resp.status_code, resp.text[:200])
        raise VoiceInputUnavailable(f"the hearing model returned {resp.status_code}")
    try:
        return resp.json()["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    except (KeyError, IndexError, TypeError, ValueError):
        raise VoiceInputUnavailable("the hearing model sent an answer this can't read") from None


async def _sarvam(cfg: dict[str, Any], key: str, audio: bytes, content_type: str, langs: list[str]) -> str:
    model = cfg.get("model") or "saaras:v4"
    form = {"model": model, "language_code": "unknown" if len(langs) > 1 else f"{langs[0]}-IN"}
    mode = (cfg.get("extra") or {}).get("mode")
    if mode and model == "saaras:v3":
        form["mode"] = mode
    ext = content_type.split("/")[1].replace("x-", "")
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            "https://api.sarvam.ai/speech-to-text", headers={"api-subscription-key": key},
            data=form, files={"file": (f"clip.{ext}", audio, content_type)},
        )
    if resp.status_code != 200:
        log.warning("voice input: Sarvam returned %s: %s", resp.status_code, resp.text[:200])
        raise VoiceInputUnavailable(f"the hearing model returned {resp.status_code}")
    try:
        return (resp.json().get("transcript") or "").strip()
    except ValueError:
        raise VoiceInputUnavailable("the hearing model sent an answer this can't read") from None


_ENGINES = {"deepgram": _deepgram, "sarvam": _sarvam}


def _platform_hearing(langs: list[str]) -> dict[str, Any] | None:
    """The platform's recommended cloud hearing model for these languages, as an in-memory config (nothing saved)."""
    from . import platform_ai  # platform_ai imports provider_configs, like this module

    pick = platform_ai.recommended("stt", langs, False)
    if pick is None:
        return None
    option, _voice = pick
    return {"engine": option.engine, "model": option.model, "extra": dict(option.extra),
            "api_key_ref": f"env:{option.key_env}", "label": f"{option.provider} {option.label}"}


async def transcribe_clip(
    tenant: dict[str, Any], agent: dict[str, Any], audio: bytes, content_type: str, *, secret_resolver: SecretResolver,
) -> dict[str, Any]:
    """{"text", "heard_by", "fallback"}: the agent's own cloud hearing model, else the platform's recommended one."""
    content_type = content_type.split(";")[0].strip().lower()
    if content_type not in ALLOWED_TYPES:
        raise VoiceInputUnavailable("unsupported recording format")
    if not audio:
        raise VoiceInputUnavailable("the recording was empty")
    if len(audio) > MAX_CLIP_BYTES:
        raise VoiceInputUnavailable("the recording is too long; keep it under a minute")
    langs = _languages(agent)
    stt_id = agent.get("stt_config_id") or tenant.get("default_stt_config_id")
    cfg = await get_provider_config(stt_id) if stt_id else None
    if cfg is not None and (str(cfg["tenant_id"]) != str(tenant["id"]) or cfg["role"] != "stt"):
        cfg = None
    fallback = cfg is None or cfg["engine"] not in _ENGINES or not cfg.get("api_key_ref")
    if fallback:
        # Local Whisper runs only inside the Conversation service; borrow the platform's cloud pick instead.
        cfg = _platform_hearing(langs)
        if cfg is None:
            raise VoiceInputUnavailable(
                "voice typing needs a cloud hearing model, and none is set up on this platform for these languages"
            )
    try:
        key = await secret_resolver.resolve(cfg["api_key_ref"])
    except (KeyError, ValueError) as exc:
        raise VoiceInputUnavailable("hearing model credentials not available") from exc
    try:
        text = await _ENGINES[cfg["engine"]](cfg, key, audio, content_type, langs)
    except httpx.RequestError as exc:
        raise VoiceInputUnavailable(f"could not reach the hearing model: {exc.__class__.__name__}") from exc
    return {"text": text, "heard_by": cfg.get("label") or cfg.get("name") or cfg["engine"], "fallback": fallback}
