"""Creates, caches and prewarms STT/LLM/TTS instances per provider_configs row.
Secrets are resolved once at instantiation, never per call."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

# TTS speed multiplier from extra["speed"]; range matches ElevenLabs. Invalid -> default.
VOICE_SPEED_MIN     = 0.7
VOICE_SPEED_DEFAULT = 1.0
VOICE_SPEED_MAX     = 1.2


def voice_speed(cfg: "ProviderConfig") -> float:
    raw = (cfg.extra or {}).get("speed", VOICE_SPEED_DEFAULT)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = -1.0
    if VOICE_SPEED_MIN <= v <= VOICE_SPEED_MAX:
        return v
    log.warning(
        "provider_config id=%s engine=%s: extra.speed=%r out of bounds "
        "[%.1f, %.1f] — using default %.1f",
        cfg.id, cfg.engine, raw, VOICE_SPEED_MIN, VOICE_SPEED_MAX, VOICE_SPEED_DEFAULT,
    )
    return VOICE_SPEED_DEFAULT


_THINK_ABSENT = object()

# Models whose Ollama default is thinking ON (5-8s/turn); absent extra.think means False
# for these. Other models omit the key.
_THINKING_CAPABLE_MODEL_PREFIXES = ("gemma4",)


def _is_thinking_capable(model: str | None) -> bool:
    return bool(model) and model.startswith(_THINKING_CAPABLE_MODEL_PREFIXES)


def _think_flag(cfg: "ProviderConfig") -> bool | None:
    safe_default = False if _is_thinking_capable(cfg.model) else None
    raw = (cfg.extra or {}).get("think", _THINK_ABSENT)
    if raw is _THINK_ABSENT:
        return safe_default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    log.warning(
        "provider_config id=%s engine=%s: extra.think=%r is not a bool — using %r",
        cfg.id, cfg.engine, raw, safe_default,
    )
    return safe_default


# Fallback for every LLM factory below when extra has no "system" override.
_VOICE_SYSTEM_PROMPT = (
    "You are a helpful voice assistant. "
    "Keep responses concise and natural for speech."
)


@dataclass(frozen=True)
class ProviderConfig:
    """The subset of a provider_configs row AIProviderManager needs."""

    id:          str
    role:        str            # 'stt' | 'llm' | 'tts'
    engine:      str
    model:       str | None = None
    voice:       str | None = None
    language:    str | None = None
    api_key_ref: str | None = None
    extra:       dict[str, Any] = field(default_factory=dict)


# factory(cfg, resolved_api_key) -> a ready-to-use provider instance (already
# .load()-ed / connected, if that provider type needs it).
ProviderFactory = Callable[[ProviderConfig, str | None], Awaitable[Any]]


async def _make_faster_whisper(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.stt.faster_whisper import FasterWhisperSTT

    inst = FasterWhisperSTT(
        model_size=cfg.model or "small",
        device=cfg.extra.get("device", "cpu"),
        compute_type=cfg.extra.get("compute_type", "int8"),
        language=cfg.language,
    )
    await inst.load()
    return inst


async def _make_ollama(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.llm.ollama import OllamaLLM

    return OllamaLLM(
        model=_model_or_default(cfg, "llama3.2"),
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
        base_url=cfg.extra.get("base_url", "http://localhost:11434"),
        timeout_s=cfg.extra.get("timeout_s", 30.0),
        think=_think_flag(cfg),
    )


async def _make_macos_tts(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.tts.macos import MacOSTTS

    # Legacy extra["wpm"] (absolute words/min) wins when present; otherwise
    # the uniform speed multiplier scales the 180 wpm default.
    wpm = cfg.extra.get("wpm") or round(180 * voice_speed(cfg))
    return MacOSTTS(voice=cfg.voice or "Samantha", speed=int(wpm))


async def _make_kokoro_tts(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.tts.kokoro import KokoroTTS

    return KokoroTTS(
        voice=cfg.voice or "af_sarah",
        speed=voice_speed(cfg),
        lang_code=cfg.extra.get("lang_code", "a"),
    )


def _model_or_default(cfg: ProviderConfig, default: str) -> str:
    """Model or engine default; logs the fallback since many rows have no model set."""
    if cfg.model:
        return cfg.model
    log.info(
        "provider_config id=%s engine=%s: no model set — using engine default %s",
        cfg.id, cfg.engine, default,
    )
    return default


def _require_api_key(cfg: ProviderConfig, api_key: str | None) -> str:
    # Fail clearly at instantiation instead of an opaque httpx header error later.
    if not api_key:
        raise ValueError(
            f"provider_config id={cfg.id!r} role={cfg.role!r} engine={cfg.engine!r} "
            "has no api_key_ref configured — cloud engines require one"
        )
    return api_key


async def _make_deepgram(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.stt.deepgram import DeepgramSTT

    return DeepgramSTT(
        api_key=_require_api_key(cfg, api_key),
        model=cfg.model or "nova-3",
        language=cfg.language,
    )


async def _make_openai_llm(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.llm.openai import OpenAILLM

    return OpenAILLM(
        api_key=_require_api_key(cfg, api_key),
        model=_model_or_default(cfg, "gpt-4o-mini"),
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
    )


async def _make_groq_llm(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.llm.openai import OpenAILLM

    # Groq's API is OpenAI-compatible.
    return OpenAILLM(
        api_key=_require_api_key(cfg, api_key),
        model=_model_or_default(cfg, "llama-3.3-70b-versatile"),
        base_url="https://api.groq.com/openai",
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
    )


async def _make_gemini_llm(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.llm.gemini import GeminiLLM

    return GeminiLLM(
        api_key=_require_api_key(cfg, api_key),
        model=_model_or_default(cfg, "gemini-flash-latest"),
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
    )


async def _make_anthropic_llm(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.llm.anthropic import AnthropicLLM

    # Haiku by default: a turn here is a sentence or two, so the pricier
    # models stay an explicit opt-in.
    return AnthropicLLM(
        api_key=_require_api_key(cfg, api_key),
        model=_model_or_default(cfg, "claude-haiku-4-5"),
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
        max_tokens=cfg.extra.get("max_tokens", 4096),
    )


async def _make_nvidia_llm(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.llm.openai import OpenAILLM

    # NVIDIA NIM is OpenAI-compatible.
    return OpenAILLM(
        api_key=_require_api_key(cfg, api_key),
        model=_model_or_default(cfg, "meta/llama-3.1-8b-instruct"),
        base_url="https://integrate.api.nvidia.com",
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
    )


async def _make_cohere_llm(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.llm.openai import OpenAILLM

    # Cohere's Compatibility API is OpenAI-compatible.
    return OpenAILLM(
        api_key=_require_api_key(cfg, api_key),
        model=_model_or_default(cfg, "command-r7b-12-2024"),
        base_url="https://api.cohere.ai/compatibility",
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
    )


async def _make_elevenlabs_tts(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.tts.elevenlabs import ElevenLabsTTS

    return ElevenLabsTTS(
        api_key=_require_api_key(cfg, api_key),
        voice_id=cfg.voice or "",
        model_id=cfg.extra.get("model_id", "eleven_turbo_v2_5"),
        speed=voice_speed(cfg),
        language_code=cfg.language,
    )


async def _make_deepgram_tts(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.tts.deepgram import DeepgramTTS

    return DeepgramTTS(
        api_key=_require_api_key(cfg, api_key),
        voice=cfg.voice or "aura-asteria-en",
    )


_DEFAULT_REGISTRY: dict[tuple[str, str], ProviderFactory] = {
    ("stt", "faster_whisper"): _make_faster_whisper,
    ("stt", "deepgram"):       _make_deepgram,
    ("llm", "ollama"):         _make_ollama,
    ("llm", "openai"):         _make_openai_llm,
    ("llm", "gemini"):         _make_gemini_llm,
    ("llm", "groq"):           _make_groq_llm,
    ("llm", "anthropic"):      _make_anthropic_llm,
    ("llm", "nvidia"):         _make_nvidia_llm,
    ("llm", "cohere"):         _make_cohere_llm,
    ("tts", "macos"):          _make_macos_tts,
    ("tts", "kokoro"):         _make_kokoro_tts,
    ("tts", "elevenlabs"):     _make_elevenlabs_tts,
    ("tts", "deepgram"):       _make_deepgram_tts,
}


class AIProviderManager:
    def __init__(
        self,
        secret_resolver: SecretResolver,
        registry: dict[tuple[str, str], ProviderFactory] | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._registry = dict(_DEFAULT_REGISTRY) if registry is None else dict(registry)
        self._instances: dict[str, Any] = {}
        # Per-config-id locks, not one global lock — creating provider A must
        # never block a concurrent request for already-cached provider B.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, cfg: ProviderConfig) -> Any:
        cached = self._instances.get(cfg.id)
        if cached is not None:
            return cached

        async with self._locks[cfg.id]:
            cached = self._instances.get(cfg.id)  # re-check: lost the race while waiting
            if cached is not None:
                return cached

            factory = self._registry.get((cfg.role, cfg.engine))
            if factory is None:
                raise ValueError(
                    f"no provider factory registered for role={cfg.role!r} engine={cfg.engine!r}"
                )

            api_key = await self._secret_resolver.resolve(cfg.api_key_ref) if cfg.api_key_ref else None
            instance = await factory(cfg, api_key)
            self._instances[cfg.id] = instance
            return instance

    async def get_stt(self, cfg: ProviderConfig) -> Any:
        assert cfg.role == "stt", f"get_stt() called with role={cfg.role!r}"
        return await self.get(cfg)

    async def get_llm(self, cfg: ProviderConfig) -> Any:
        assert cfg.role == "llm", f"get_llm() called with role={cfg.role!r}"
        return await self.get(cfg)

    async def get_tts(self, cfg: ProviderConfig) -> Any:
        assert cfg.role == "tts", f"get_tts() called with role={cfg.role!r}"
        return await self.get(cfg)

    async def prewarm(self, configs: Iterable[ProviderConfig]) -> None:
        """Instantiate every given provider up front so calls never pay load cost."""
        for cfg in configs:
            await self.get(cfg)

    def cached_ids(self) -> frozenset[str]:
        """For tests/introspection — not used on any call path."""
        return frozenset(self._instances.keys())

    def invalidate(self, config_id: str) -> bool:
        """Evict one cached instance; returns whether it was cached. Not closed,
        since a live call may still hold a reference to it."""
        return self._instances.pop(config_id, None) is not None
