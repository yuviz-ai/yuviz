"""Creates, caches and prewarms STT/LLM/TTS instances per provider_configs row.
Secrets are resolved once at instantiation, never per call."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

from libs.config_sdk.languages import KOKORO_DEFAULT_VOICE
from libs.config_sdk.local_models import find_local_model

from .providers.tts.kokoro import KOKORO_REPO
from .secret_resolver import SecretResolver

log = logging.getLogger(__name__)


class ModelNotReadyError(RuntimeError):
    """A local model is not on this host's disk yet (still downloading, or never fetched).
    `provider` is the instance built anyway, for a caller with no better provider to use: it
    loads or fails lazily, as it did before this check existed."""

    def __init__(self, engine: str, model: str, provider: Any = None):
        super().__init__(f"{engine} model {model!r} is not downloaded on this host")
        self.engine = engine
        self.model = model
        self.provider = provider


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


# Whisper instances whose model was missing, keyed by what identifies the model: the next call
# finds the same one (already downloading) instead of starting another download.
_not_ready_whisper: dict[tuple[str, str, str, str | None], Any] = {}


async def _make_faster_whisper(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.stt.faster_whisper import FasterWhisperSTT

    model_size = cfg.model or "small"
    device = cfg.extra.get("device", "cpu")
    compute_type = cfg.extra.get("compute_type", "int8")
    key = (model_size, device, compute_type, cfg.language)  # everything that shapes the instance
    inst = _not_ready_whisper.get(key)
    if inst is not None and inst.loaded:
        del _not_ready_whisper[key]
        return inst
    if inst is not None:
        inst.load_when_needed()  # restarts a background load that failed
    if inst is None:
        inst = FasterWhisperSTT(
            model_size=model_size, device=device, compute_type=compute_type, language=cfg.language,
        )
        try:
            await inst.load()
        except LocalEntryNotFoundError:
            if find_local_model("faster_whisper", model_size) is None:
                raise  # only registry models are downloaded: the name is tenant-chosen
            # Pre-check behaviour: the model downloads on its first use. Until it has, the instance
            # transcribes nothing, so a caller with a better provider is given that one.
            inst.load_when_needed()
            _not_ready_whisper[key] = inst
        else:
            return inst
    raise ModelNotReadyError("faster_whisper", model_size, provider=inst)


async def close_not_ready_providers() -> None:
    """At shutdown: stop the background model downloads of instances no call has taken over."""
    for inst in (*_not_ready_whisper.values(), *_not_ready_kokoro.values()):
        await inst.stop_download()


async def _make_ollama(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.llm.ollama import OllamaLLM

    llm = OllamaLLM(
        model=_model_or_default(cfg, "llama3.2"),
        system=cfg.extra.get("system", _VOICE_SYSTEM_PROMPT),
        temperature=cfg.extra.get("temperature", 0.7),
        base_url=cfg.extra.get("base_url") or os.environ.get("VOICEAI_LLM_URL") or "http://localhost:11434",
        timeout_s=cfg.extra.get("timeout_s", 30.0),
        think=_think_flag(cfg),
    )
    # Checked once per cached provider, not per turn.
    if not await llm.model_pulled():
        raise ModelNotReadyError("ollama", llm._model, provider=llm)
    return llm


async def _make_macos_tts(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.tts.macos import MacOSTTS

    # Legacy extra["wpm"] (absolute words/min) wins when present; otherwise
    # the uniform speed multiplier scales the 180 wpm default.
    wpm = cfg.extra.get("wpm") or round(180 * voice_speed(cfg))
    return MacOSTTS(voice=cfg.voice or "Samantha", speed=int(wpm))


# One deferred instance per voice while the base weights are missing, so N concurrent calls
# don't each load their own copy of the model on first synthesis.
_not_ready_kokoro: dict[tuple[str, float, str], Any] = {}


async def _make_kokoro_tts(cfg: ProviderConfig, _api_key: str | None) -> Any:
    from .providers.tts.kokoro import KokoroTTS

    voice = cfg.voice or KOKORO_DEFAULT_VOICE
    speed, lang_code = voice_speed(cfg), cfg.extra.get("lang_code", "a")
    key = (voice, speed, lang_code)
    # Checked before anything heavy is built: KPipeline would download the base weights
    # (~330 MB) synchronously on the event loop. A voice file is ~0.5 MB: KPipeline fetches
    # it on first use.
    try:
        for filename in ("config.json", "kokoro-v1_0.pth"):
            await asyncio.to_thread(hf_hub_download, KOKORO_REPO, filename, local_files_only=True)
    except LocalEntryNotFoundError as exc:
        # For a caller with nothing better: loads (and downloads) on its first synthesis, off the loop.
        tts = _not_ready_kokoro.get(key)
        if tts is None:
            tts = _not_ready_kokoro[key] = KokoroTTS(voice=voice, speed=speed, lang_code=lang_code, defer_load=True)
        raise ModelNotReadyError("kokoro", KOKORO_REPO, provider=tts) from exc
    pending = _not_ready_kokoro.pop(key, None)
    if pending is not None and pending.loaded:
        return pending
    # The ~1-3 s torch load runs in the executor, here at prewarm, not on a live call's greeting.
    tts = KokoroTTS(voice=voice, speed=speed, lang_code=lang_code, defer_load=True)
    await tts.load()
    return tts


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


def _optional_ms(cfg: ProviderConfig, key: str, *, low: int, high: int) -> int | None:
    """A whole-number millisecond setting from provider extra, or None when unset."""
    value = (cfg.extra or {}).get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(
            f"provider_config id={cfg.id!r} engine={cfg.engine!r} has an invalid extra.{key} {value!r} "
            f"— expected whole milliseconds between {low} and {high}"
        )
    return int(value)


async def _make_deepgram(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.stt.deepgram import DeepgramSTT

    return DeepgramSTT(
        api_key=_require_api_key(cfg, api_key),
        model=cfg.model or "nova-3",
        language=cfg.language,
        endpointing_ms=_optional_ms(cfg, "endpointing_ms", low=10, high=5000),
    )


async def _make_sarvam_stt(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.stt.sarvam import DEFAULT_MODEL, SarvamSTT

    return SarvamSTT(
        api_key=_require_api_key(cfg, api_key),
        model=cfg.model or DEFAULT_MODEL,
        language=cfg.language,
        mode=(cfg.extra or {}).get("mode"),
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


async def _make_sarvam_tts(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.tts.sarvam import DEFAULT_MODEL, DEFAULT_SPEAKER, SarvamTTS

    # The voice is a Sarvam speaker name (lowercase, and it must belong to the model).
    return SarvamTTS(
        api_key=_require_api_key(cfg, api_key),
        speaker=(cfg.voice or "").strip().lower() or DEFAULT_SPEAKER,
        model=cfg.extra.get("model") or cfg.model or DEFAULT_MODEL,
        pace=voice_speed(cfg),
        language=cfg.language,
        # extra.streaming = false falls back to whole-sentence REST synthesis.
        streaming=cfg.extra.get("streaming", True) is not False,
    )


async def _make_deepgram_tts(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.tts.deepgram import DeepgramTTS

    return DeepgramTTS(
        api_key=_require_api_key(cfg, api_key),
        voice=cfg.voice or "aura-asteria-en",
    )


async def _make_cartesia_tts(cfg: ProviderConfig, api_key: str | None) -> Any:
    from .providers.tts.cartesia import CartesiaTTS

    # voice is a Cartesia voice id (a UUID), not a display name — there is
    # no sensible default, so an unset one fails here rather than
    # synthesizing in whatever voice the API happens to pick.
    voice = (cfg.voice or "").strip()
    if not voice:
        raise ValueError(
            f"provider_config id={cfg.id!r} engine='cartesia' has no voice set — "
            "Cartesia needs a voice id (a UUID from its /voices list)"
        )
    extra = cfg.extra or {}
    speed = extra.get("speed")
    if speed is not None:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            speed = float("nan")
        if isinstance(extra["speed"], bool) or not math.isfinite(speed):
            raise ValueError(
                f"provider_config id={cfg.id!r} engine='cartesia' has a non-numeric "
                f"extra.speed {extra['speed']!r} — expected a multiplier such as 0.85"
            )
    return CartesiaTTS(
        api_key=_require_api_key(cfg, api_key),
        voice=voice,
        model=str(extra.get("model") or "sonic-2"),
        speed=speed,
        language=cfg.language,
    )


_DEFAULT_REGISTRY: dict[tuple[str, str], ProviderFactory] = {
    ("stt", "faster_whisper"): _make_faster_whisper,
    ("stt", "deepgram"):       _make_deepgram,
    ("stt", "sarvam"):         _make_sarvam_stt,
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
    ("tts", "sarvam"):         _make_sarvam_tts,
    ("tts", "cartesia"):       _make_cartesia_tts,
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
