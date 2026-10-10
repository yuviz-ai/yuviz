"""
Turns resolved provider config rows into live ISTT/ILLM/ITTS instances.
Lives here, not in libs/config_sdk, so the SDK never depends on services/conversation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs

from .ai_provider_manager import AIProviderManager, ProviderConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderBundle:
    """Live provider instances for one call."""
    stt: Any
    llm: Any
    tts: Any
    # Per-language voice overrides (multilingual agents); absent language = `tts`.
    tts_by_language: dict[str, Any] = field(default_factory=dict)

    def tts_for(self, language: str | None) -> Any:
        return self.tts_by_language.get(language, self.tts) if language else self.tts


def _to_ai_provider_config(cfg: SDKProviderConfig) -> ProviderConfig:
    """Convert the SDK's public ProviderConfig to AIProviderManager's internal one."""
    return ProviderConfig(
        id=cfg.id,
        role=cfg.role,
        engine=cfg.engine,
        model=cfg.model,
        voice=cfg.voice,
        language=cfg.language,
        api_key_ref=cfg.api_key_ref,
        extra=cfg.extra,
    )


class ProviderRegistry:
    """Resolves ProviderConfigs into a ProviderBundle via AIProviderManager's cache."""

    def __init__(self, manager: AIProviderManager) -> None:
        self._manager = manager

    async def resolve(self, providers: ProviderConfigs) -> ProviderBundle:
        from .providers.llm.retry import RetryOnceLLM

        stt = await self._manager.get(_to_ai_provider_config(providers.stt))
        llm = await self._manager.get(_to_ai_provider_config(providers.llm))
        tts = await self._manager.get(_to_ai_provider_config(providers.tts))
        llm = RetryOnceLLM(llm, name=f"{providers.llm.engine}:{providers.llm.model}")
        # Overrides were tenant-checked by the Config SDK; instances are cached per
        # provider_configs.id like any other, so this is call setup, never a turn.
        overrides = getattr(providers, "tts_by_language", None) or {}
        tts_by_language: dict[str, Any] = {}
        if overrides:
            langs = list(overrides)
            instances = await asyncio.gather(
                *(self._manager.get(_to_ai_provider_config(overrides[lang])) for lang in langs),
                return_exceptions=True,
            )
            for lang, inst in zip(langs, instances):
                if isinstance(inst, BaseException):
                    # A broken override must not fail the call; that language uses the base voice.
                    log.error(
                        "TTS override for %s (provider_config %s) failed to load — using the base voice",
                        lang, overrides[lang].id, exc_info=inst,
                    )
                    continue
                tts_by_language[lang] = inst
        return ProviderBundle(stt=stt, llm=llm, tts=tts, tts_by_language=tts_by_language)
