"""
Turns resolved provider config rows into live ISTT/ILLM/ITTS instances.
Lives here, not in libs/config_sdk, so the SDK never depends on services/conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from libs.config_sdk import ProviderConfig as SDKProviderConfig
from libs.config_sdk import ProviderConfigs

from .ai_provider_manager import AIProviderManager, ProviderConfig


@dataclass(frozen=True)
class ProviderBundle:
    """Live provider instances for one call."""
    stt: Any
    llm: Any
    tts: Any


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
        return ProviderBundle(stt=stt, llm=llm, tts=tts)
