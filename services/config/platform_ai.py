"""Platform-managed AI: the cloud models Yuviz runs on its own keys (root .env), plus local models a superadmin can
turn on. Choosing one find-or-creates a locked provider_configs row in the workspace (extra.managed = option id, key an
env: pointer), so agents keep same-tenant FKs and the call path is unchanged."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from libs.config_sdk.languages import deepgram_supports_multi, stt_languages, tts_languages, tts_model_of
from libs.config_sdk.local_models import LOCAL_MODELS, find_local_model
from libs.tenancy import platform_conn, tenant_conn

from . import db, provider_configs
from .provider_configs import MANAGED_KEY

PLATFORM_FEE_PER_MIN = 0.02  # Yuviz margin placeholder until billing; not a vendor price

_VOICE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


@dataclass(frozen=True)
class AiOption:
    id: str
    role: str
    engine: str
    provider: str
    label: str
    model: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    key_env: str | None = None
    price_per_min: float = 0.0
    default_voice: str | None = None
    blurb: str = ""

    @property
    def local(self) -> bool:
        return self.key_env is None

    def languages(self, voice: str | None = None) -> frozenset[str]:
        if self.role == "stt":
            return stt_languages(self.engine, self.model)
        if self.role == "tts":
            return tts_languages(self.engine, tts_model_of(self.engine, self.model, self.extra), voice or self.default_voice)
        return frozenset()


# Per-minute estimates from public list prices (pay-as-you-go, checked 2026-10-30), for the cost bar until billing.
# STT bills the whole call; LLM and TTS are converted with the assumptions below.
INR_PER_USD = 88.0  # Sarvam prices in INR
_TURNS_PER_MIN = 4
_INPUT_TOKENS_PER_TURN = 2000  # system prompt + history, no prompt caching
_OUTPUT_TOKENS_PER_TURN = 60
_TTS_CHARS_PER_MIN = 780 * 0.5  # ~780 chars per spoken minute, agent speaking half the call


def _llm(input_per_mtok: float, output_per_mtok: float) -> float:
    tokens_in, tokens_out = _TURNS_PER_MIN * _INPUT_TOKENS_PER_TURN, _TURNS_PER_MIN * _OUTPUT_TOKENS_PER_TURN
    return round((tokens_in * input_per_mtok + tokens_out * output_per_mtok) / 1e6, 4)


def _tts(usd_per_1k_chars: float) -> float:
    return round(usd_per_1k_chars * _TTS_CHARS_PER_MIN / 1000, 4)


def _cloud(role, engine, provider, label, model, key_env, price, *, extra=None, voice=None, blurb=""):
    return AiOption(f"{role}:{engine}:{model}", role, engine, provider, label, model, extra or {}, key_env, price, voice, blurb)


_CLOUD: tuple[AiOption, ...] = (
    _cloud("llm", "openai", "OpenAI", "GPT-4o mini", "gpt-4o-mini", "OPENAI_API_KEY", _llm(0.15, 0.60), blurb="Fast and low cost."),
    _cloud("llm", "openai", "OpenAI", "GPT-4o", "gpt-4o", "OPENAI_API_KEY", _llm(2.50, 10.00), blurb="Smarter, slower."),
    _cloud("llm", "gemini", "Gemini", "Gemini Flash", "gemini-flash-latest", "GEMINI_API_KEY", _llm(0.75, 3.75), blurb="Fast and low cost."),
    _cloud("llm", "gemini", "Gemini", "Gemini 2.5 Pro", "gemini-2.5-pro", "GEMINI_API_KEY", _llm(1.25, 10.00), blurb="Smarter, slower."),
    _cloud("llm", "anthropic", "Anthropic", "Claude Haiku 4.5", "claude-haiku-4-5", "ANTHROPIC_API_KEY", _llm(1.00, 5.00), blurb="Careful and quick."),
    _cloud("llm", "anthropic", "Anthropic", "Claude Sonnet 5", "claude-sonnet-5", "ANTHROPIC_API_KEY", _llm(2.00, 10.00), blurb="Strongest reasoning."),
    _cloud("llm", "groq", "Groq", "Llama 3.3 70B", "llama-3.3-70b-versatile", "GROQ_API_KEY", _llm(0.59, 0.79), blurb="Very fast replies."),
    _cloud("llm", "groq", "Groq", "Llama 3.1 8B", "llama-3.1-8b-instant", "GROQ_API_KEY", _llm(0.05, 0.08), blurb="Fastest, simple calls."),
    _cloud("stt", "deepgram", "Deepgram", "Nova-3", "nova-3", "DEEPGRAM_API_KEY", 0.0077, blurb="Most accurate, switches languages."),
    _cloud("stt", "deepgram", "Deepgram", "Nova-2", "nova-2", "DEEPGRAM_API_KEY", round(0.35 / 60, 4), blurb="Lower cost."),
    _cloud("stt", "sarvam", "Sarvam", "Saaras v4 Realtime", "saaras:v4", "SARVAM_API_KEY", round(0.49 / INR_PER_USD, 4),
           blurb="Made for Indian callers, Hindi and English."),
    _cloud("stt", "sarvam", "Sarvam", "Saaras v3", "saaras:v3", "SARVAM_API_KEY", round(0.49 / INR_PER_USD, 4),
           blurb="Indian languages, with code-mix output."),
    _cloud("tts", "elevenlabs", "ElevenLabs", "Flash v2.5", "eleven_flash_v2_5", "ELEVENLABS_API_KEY", _tts(0.04),
           extra={"model_id": "eleven_flash_v2_5"}, voice="EXAVITQu4vr4xnSDxMaL", blurb="Lifelike and fast."),
    _cloud("tts", "elevenlabs", "ElevenLabs", "Multilingual v2", "eleven_multilingual_v2", "ELEVENLABS_API_KEY", _tts(0.08),
           extra={"model_id": "eleven_multilingual_v2"}, voice="EXAVITQu4vr4xnSDxMaL", blurb="Most expressive, slower."),
    _cloud("tts", "cartesia", "Cartesia", "Sonic 2", "sonic-2", "CARTESIA_API_KEY", _tts(0.039),
           extra={"model": "sonic-2"}, blurb="Very fast, natural."),
    _cloud("tts", "sarvam", "Sarvam", "Bulbul v4 Flash", "bulbul:v4-flash", "SARVAM_API_KEY", _tts(1.00 / INR_PER_USD),
           voice="shubh_enhi_ads", blurb="Made for Indian languages."),
    _cloud("tts", "sarvam", "Sarvam", "Bulbul v3", "bulbul:v3", "SARVAM_API_KEY", _tts(3.00 / INR_PER_USD),
           voice="shubh", blurb="Natural Indian voices."),
    _cloud("tts", "deepgram", "Deepgram", "Aura", "aura", "DEEPGRAM_API_KEY", _tts(0.015),
           voice="aura-asteria-en", blurb="Low cost, English only."),
)

# From Sarvam's text-to-speech API reference.
SARVAM_V3_SPEAKERS = frozenset({
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan", "simran", "kavya", "amit", "dev", "ishita",
    "shreya", "ratan", "varun", "manan", "sumit", "roopa", "kabir", "aayan", "ashutosh", "advait", "anand", "tanya",
    "tarun", "sunny", "mani", "gokul", "vijay", "shruti", "suhani", "mohit", "kavitha", "rehan", "soham", "rupali",
})

_KOKORO_DEFAULT_BY_LANGUAGE = {
    "en": "af_sarah", "hi": "hf_alpha", "es": "ef_dora", "fr": "ff_siwis", "it": "if_sara",
    "ja": "jf_alpha", "pt": "pf_dora", "zh": "zf_xiaobei",
}

_LOCAL: tuple[AiOption, ...] = (
    *(AiOption(f"llm:ollama:{m.name}", "llm", "ollama", "Ollama", m.name, m.name, blurb="Runs on your server.")
      for m in LOCAL_MODELS if m.engine == "ollama"),
    *(AiOption(f"stt:faster_whisper:{n}", "stt", "faster_whisper", "Whisper", f"Whisper {n}", n, blurb="Runs on your server.")
      for n in ("small", "base", "base.en", "medium")),
    AiOption("tts:kokoro:kokoro", "tts", "kokoro", "Kokoro", "Kokoro", None, default_voice="af_sarah", blurb="Runs on your server."),
)

OPTIONS: dict[str, AiOption] = {o.id: o for o in (*_CLOUD, *_LOCAL)}


def key_configured(option: AiOption) -> bool:
    return option.local or bool(os.environ.get(option.key_env or ""))


async def get_settings() -> dict[str, Any]:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="platform-ai-settings-read") as conn:
        row = await conn.fetchrow("SELECT local_models_enabled, updated_at FROM platform_ai_settings")
    return dict(row) if row else {"local_models_enabled": False, "updated_at": None}


async def set_settings(*, local_models_enabled: bool, user_id: Any) -> dict[str, Any]:
    pool = await db.get_pool()
    async with platform_conn(pool, reason="platform-ai-settings-write") as conn:
        row = await conn.fetchrow(
            "INSERT INTO platform_ai_settings (singleton, local_models_enabled, updated_at, updated_by) "
            "VALUES (true, $1, now(), $2) ON CONFLICT (singleton) DO UPDATE "
            "SET local_models_enabled = EXCLUDED.local_models_enabled, updated_at = now(), updated_by = EXCLUDED.updated_by "
            "RETURNING local_models_enabled, updated_at",
            local_models_enabled, user_id,
        )
    return dict(row)


def available(option: AiOption, local_enabled: bool) -> bool:
    return (local_enabled if option.local else True) and key_configured(option)


def catalog(local_enabled: bool) -> list[dict[str, Any]]:
    """Every option a workspace may see: local ones only when enabled. Unconfigured cloud keys stay listed, unavailable."""
    out = []
    for o in OPTIONS.values():
        if o.local and not local_enabled:
            continue
        out.append({
            "id": o.id, "role": o.role, "engine": o.engine, "provider": o.provider, "label": o.label, "model": o.model,
            "price_per_min": o.price_per_min, "default_voice": o.default_voice, "blurb": o.blurb, "local": o.local,
            "available": key_configured(o), "languages": sorted(o.languages()),
        })
    return out


def _covers(option: AiOption, languages: list[str], voice: str | None = None) -> bool:
    if not set(languages) <= option.languages(voice):
        return False
    if option.role == "stt" and option.engine == "deepgram" and len(languages) > 1:
        return deepgram_supports_multi(option.model, languages)
    return True


def recommended(role: str, languages: list[str], local_enabled: bool) -> tuple[AiOption, str | None] | None:
    """The first available option for `role` that covers the languages (TTS: the main language at least)."""
    candidates = [o for o in OPTIONS.values() if o.role == role and available(o, local_enabled)]
    if role == "tts":
        # Sarvam first for Indian-language callers; Cartesia has no default voice to start from.
        candidates.sort(key=lambda o: (o.engine != "sarvam") if "hi" in languages else (o.engine == "sarvam"))
        candidates = [o for o in candidates if o.default_voice or o.engine == "kokoro"]
        for need in (languages, languages[:1]):
            for o in candidates:
                voice = _KOKORO_DEFAULT_BY_LANGUAGE.get(need[0]) if o.engine == "kokoro" else o.default_voice
                if voice and _covers(o, need, voice):
                    return o, voice
        return None
    for o in candidates:
        if role == "llm" or _covers(o, languages):
            return o, None
    return None


def _validate_voice(option: AiOption, voice: str | None) -> str | None:
    if option.role != "tts":
        return None
    voice = (voice or option.default_voice or "").strip()
    if not voice:
        raise ValueError(f"{option.provider} needs a voice; pick one")
    if not _VOICE_RE.match(voice):
        raise ValueError("voice id has unsupported characters")
    if option.engine == "kokoro" and find_local_model("kokoro", voice) is None:
        raise ValueError(f"voice {voice!r} is not a Kokoro voice")
    # Sarvam speakers belong to one model; a mismatch fails at call time.
    if option.model == "bulbul:v3" and voice not in SARVAM_V3_SPEAKERS:
        raise ValueError(f"voice {voice!r} is not a Bulbul v3 speaker")
    if option.model == "bulbul:v4-flash" and not re.fullmatch(r"[a-z]+_[a-z]+_[a-z]+", voice):
        raise ValueError(f"voice {voice!r} is not a Bulbul v4 Flash persona")
    return voice


def _validate_temperature(option: AiOption, temperature: float | None) -> float | None:
    if temperature is None:
        return None
    if option.role != "llm":
        raise ValueError("temperature applies to language models only")
    if not 0.0 <= temperature <= 1.2:
        raise ValueError("temperature must be between 0 and 1.2")
    if option.engine == "anthropic" and any(option.model.startswith(m) for m in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5", "claude-opus-4-8", "claude-opus-4-7")):
        raise ValueError(f"{option.model} does not accept temperature; it thinks adaptively")
    return round(float(temperature), 2)


async def choose(
    tenant_id: Any, option_id: str, *, voice: str | None = None, voice_name: str | None = None,
    temperature: float | None = None, user_id: Any = None, user_email: str | None = None,
) -> dict[str, Any]:
    """The workspace's managed row for this option (and voice/temperature), created on first use."""
    option = OPTIONS.get(option_id)
    if option is None:
        raise ValueError(f"unknown AI option {option_id!r}")
    settings = await get_settings()
    if not available(option, settings["local_models_enabled"]):
        raise ValueError(f"{option.provider} {option.label} is not available on this platform yet")
    voice = _validate_voice(option, voice)
    temperature = _validate_temperature(option, temperature)
    key_ref = None if option.local else f"env:{option.key_env}"

    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT * FROM provider_configs WHERE tenant_id = $1 AND role = $2 AND deleted_at IS NULL "
            "AND extra->>'managed' = $3",
            tenant_id, option.role, option.id,
        )
    for row in rows:
        r = dict(row)
        r["extra"] = db.json_col(r["extra"])
        if r["voice"] == voice and r["api_key_ref"] == key_ref and r["extra"].get("temperature") == temperature:
            return r

    name = f"{option.provider} {option.label}"
    if voice:
        label = re.sub(r"[^\w .()'-]", "", voice_name or "")[:60].strip() or voice
        name += f" · {label}"
    if temperature is not None:
        name += f" · {temperature:g}"
    extra = {**option.extra, MANAGED_KEY: option.id}
    if temperature is not None:
        extra["temperature"] = temperature
    return await provider_configs.create_provider_config(
        tenant_id=tenant_id, name=name, role=option.role, engine=option.engine, model=option.model,
        voice=voice, api_key_ref=key_ref, extra=extra, allow_pointer_schemes=True,
        user_id=user_id, user_email=user_email,
    )



async def option_voices(option_id: str) -> list[dict[str, Any]]:
    """The voices the platform's own ElevenLabs/Cartesia account offers for this option."""
    option = OPTIONS.get(option_id)
    if option is None or option.role != "tts" or option.engine not in ("elevenlabs", "cartesia"):
        raise ValueError("voices are listed for ElevenLabs and Cartesia options only")
    api_key = os.environ.get(option.key_env or "")
    if not api_key:
        raise ValueError(f"{option.provider} is not available on this platform yet")
    return await provider_configs.fetch_vendor_voices(option.engine, api_key, source=f"platform option {option_id}")


# Fixed lines, so a preview can't be used to synthesize arbitrary text on the platform's key.
_PREVIEW_LINES = {
    "en": "Hi, thanks for calling. How can I help you today?",
    "hi": "नमस्ते, कॉल करने के लिए धन्यवाद। मैं आपकी कैसे मदद कर सकती हूँ?",
    "ta": "வணக்கம், அழைத்ததற்கு நன்றி. இன்று உங்களுக்கு எப்படி உதவலாம்?",
    "te": "నమస్కారం, కాల్ చేసినందుకు ధన్యవాదాలు. ఈ రోజు మీకు ఎలా సహాయం చేయగలం?",
    "kn": "ನಮಸ್ಕಾರ, ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ಇಂದು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "mr": "नमस्कार, कॉल केल्याबद्दल धन्यवाद. आज आम्ही तुमची कशी मदत करू शकतो?",
    "bn": "নমস্কার, কল করার জন্য ধন্যবাদ। আজ আপনাকে কীভাবে সাহায্য করতে পারি?",
    "es": "Hola, gracias por llamar. ¿En qué puedo ayudarle hoy?",
    "fr": "Bonjour, merci de votre appel. Comment puis-je vous aider ?",
    "de": "Hallo, danke für Ihren Anruf. Wie kann ich Ihnen helfen?",
    "pt": "Olá, obrigado por ligar. Como posso ajudar hoje?",
    "it": "Ciao, grazie per la chiamata. Come posso aiutarti oggi?",
    "ja": "お電話ありがとうございます。本日はどのようなご用件でしょうか。",
    "zh": "您好，感谢来电。请问有什么可以帮您？",
}
_PREVIEW_CACHE: dict[tuple[str, str, str], bytes] = {}
_PREVIEW_CACHE_MAX = 200


async def option_preview(option_id: str, voice: str, language: str, *, secret_resolver: Any) -> bytes:
    """WAV of the fixed greeting in this voice, on the platform's key; repeat plays come from memory."""
    from . import voice_preview

    option = OPTIONS.get(option_id)
    if option is None or option.role != "tts" or option.local:
        raise ValueError("previews are for the platform's cloud voices")
    if not key_configured(option):
        raise ValueError(f"{option.provider} is not available on this platform yet")
    voice = _validate_voice(option, voice)
    line = _PREVIEW_LINES.get(language) or _PREVIEW_LINES["en"]
    key = (option.id, voice, language if language in _PREVIEW_LINES else "en")
    if key in _PREVIEW_CACHE:
        return _PREVIEW_CACHE[key]
    cfg = {
        "id": f"platform option {option.id}", "role": "tts", "engine": option.engine, "model": option.model,
        "voice": voice, "extra": dict(option.extra), "language": key[2], "api_key_ref": f"env:{option.key_env}",
    }
    if option.engine == "elevenlabs" and option.extra.get("model_id") not in ("eleven_flash_v2_5", "eleven_turbo_v2_5"):
        cfg["language"] = None  # only the v2.5 models take a language_code
    try:
        wav = await voice_preview.synthesize_preview(cfg, line, secret_resolver=secret_resolver)
    except voice_preview.PreviewUnavailable as exc:
        raise ValueError(exc.detail) from None
    if len(_PREVIEW_CACHE) >= _PREVIEW_CACHE_MAX:
        _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
    _PREVIEW_CACHE[key] = wav
    return wav
