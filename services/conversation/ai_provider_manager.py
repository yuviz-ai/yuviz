"""
AIProviderManager — creates, caches, and pre-warms STT/LLM/TTS provider
instances per distinct provider_configs row.

Naming: "AI Provider Manager", not "Provider Registry" — it owns lifecycle,
caching, secret resolution, and (Phase 7) health/fallback, not just lookup.
Internally the implementation is still a registry (see _DEFAULT_REGISTRY).

Non-negotiable latency rules this module exists to satisfy:
  - Secrets are resolved once, here, at instantiation time — never per-call.
  - prewarm() instantiates+loads every configured provider at process
    startup, so the first real call never pays instantiation cost.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)

# Uniform TTS speech-rate multiplier, read from provider_configs.extra
# ["speed"] and honored by every TTS engine (kokoro natively; macos as
# wpm = round(180 * speed) unless a legacy extra["wpm"] overrides; elevenlabs
# as voice_settings.speed — the same 0.7–1.2 range ElevenLabs itself
# supports). Out-of-bounds/non-numeric falls back to the default with a
# warning — same degrade-don't-reject posture as transfer_timeout_ms.
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

# Models whose Ollama default is thinking ON (5-8s/turn, confirmed live for
# gemma4). Selecting one of these from the admin-ui catalog must not ship a
# silent 5-8x latency regression just because no one has set extra.think —
# there is no UI path to set it at all (ProvidersPanel.tsx has zero extra.*
# fields today). So absent/malformed think on one of these models degrades
# to the safe value (False), not to omitting the key. Every other engine's
# absent/malformed case still omits, matching the byte-identical payload
# the existing fleet already sends.
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
    """provider_configs.model is nullable and the admin UI's model select
    starts blank, so plenty of live rows run on whatever an engine's
    fallback happens to be — changing one silently re-points all of them
    (gpt-4o -> gpt-4o-mini here did exactly that). Log which model a row
    actually ended up on so that is visible rather than inferred."""
    if cfg.model:
        return cfg.model
    log.info(
        "provider_config id=%s engine=%s: no model set — using engine default %s",
        cfg.id, cfg.engine, default,
    )
    return default


def _require_api_key(cfg: ProviderConfig, api_key: str | None) -> str:
    # A cloud engine with no api_key_ref configured is a misconfiguration —
    # fail loudly and clearly here, at instantiation time, rather than let
    # httpx raise an opaque error deep in a header-construction call once a
    # None gets passed where a str is expected.
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

    # Groq's API is OpenAI-compatible — same class as _make_openai_llm,
    # just pointed at Groq's endpoint with a
    # Groq-hosted model default. See openai.py's module docstring.
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

    # NVIDIA's hosted NIM catalog is OpenAI-compatible — a base_url swap,
    # same as _make_groq_llm, not a new client.
    return OpenAILLM(
        api_key=_require_api_key(cfg, api_key),
        model=_model_or_default(cfg, "meta/llama-3.1-8b-instruct"),
        base_url="https://integrate.api.nvidia.com",
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
    )


async def _make_cohere_llm(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.llm.openai import OpenAILLM

    # Cohere's Compatibility API is an OpenAI-shaped front door onto the
    # same models (streaming + tools documented) — again a base_url swap.
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


# Every engine referenced in the schema/UI now has a real implementation
# registered here — local (faster_whisper/ollama/macos/kokoro) and cloud
# (deepgram/openai/elevenlabs) both go through the exact same registry
# mechanism; adding one is a new dict entry, never a change to
# AIProviderManager itself.
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
        """Call once at process startup with every provider config the
        process expects to serve, so the first real call never pays
        instantiation/model-load cost."""
        for cfg in configs:
            await self.get(cfg)

    def cached_ids(self) -> frozenset[str]:
        """For tests/introspection — not used on any call path."""
        return frozenset(self._instances.keys())

    def invalidate(self, config_id: str) -> bool:
        """Evicts one cached instance so the next get() reconstructs it
        from the (presumably just-changed) provider_configs row — called
        by provider_config_subscriber.py on a Redis Pub/Sub notification,
        not on any call path itself. Deliberately does NOT close/cleanup
        the evicted instance: a call already in progress may be holding a
        direct reference to it (handler_factory() resolves once per call
        and keeps that reference for the call's whole lifetime — see
        __main__.py), so forcibly closing it here could break a live
        call. It's simply left for garbage collection once nothing still
        references it. Returns whether anything was actually cached for
        this id (false is a normal, harmless outcome — e.g. a config that
        was never prewarmed/used yet)."""
        return self._instances.pop(config_id, None) is not None
