"""Language registry shared by the Config Service (validation) and the Conversation
Service (runtime). Adding a language is one LANGUAGES row; its spoken system
strings fall back to English until services/conversation/i18n gets a table for it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import Agent, ProviderConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Language:
    code:           str           # ISO 639-1
    name:           str           # English name, used in the LLM instruction
    native_name:    str
    kokoro_code:    str | None    # KPipeline lang_code; None = no Kokoro voice
    deepgram_multi: bool          # covered by Deepgram nova-2/3 language=multi
    # Extra LLM guidance on how to write this language so TTS reads it well.
    script_hint:    str | None = None
    # Detector codes that mean this language for us when they aren't supported themselves:
    # Whisper often labels spoken Hindi as Urdu (the two are near-identical when spoken).
    aliases:        tuple[str, ...] = ()


LANGUAGES: dict[str, Language] = {l.code: l for l in (
    Language("en", "English",    "English",   "a", True),
    Language("hi", "Hindi",      "हिन्दी",     "h", True,
             "Always write Hindi words in Devanagari script (देवनागरी), even when the caller's "
             "words reach you in Latin letters; write English words in Latin script.",
             aliases=("ur",)),
    Language("es", "Spanish",    "Español",   "e", True),
    Language("fr", "French",     "Français",  "f", True),
    Language("de", "German",     "Deutsch",   None, True),
    Language("pt", "Portuguese", "Português", "p", True),
    Language("it", "Italian",    "Italiano",  "i", True),
    # Kokoro ja/zh need misaki[ja]/misaki[zh] (pyopenjtalk, jieba, ...), which aren't installed:
    # no Kokoro code until they are, so validation never accepts a voice that would be silent.
    Language("ja", "Japanese",   "日本語",     None, True),
    Language("zh", "Chinese",    "中文",       None, False),
)}

# Deepgram's code-switching mode; a valid provider_configs.language for Deepgram STT only.
DEEPGRAM_MULTI = "multi"

# Deepgram nova models that support language=multi.
_DEEPGRAM_MULTI_MODELS = ("nova-2", "nova-3")

_LANG_TAG_RE = re.compile(r"^([A-Za-z]{2,3})(?:[-_].*)?$")


def normalize_language(code: str | None) -> str | None:
    """'hi-IN' / 'hi-Latn' / 'HI' -> 'hi'; None or unparseable -> None."""
    if not code:
        return None
    m = _LANG_TAG_RE.match(code.strip())
    return m.group(1).lower() if m else None


def resolve_alias(code: str | None, supported: tuple[str, ...] | list[str]) -> str | None:
    """A detected code mapped onto the agent's languages: itself when supported, else the
    supported language that lists it as an alias ('ur' -> 'hi' when only hi is supported)."""
    if code is None or code in supported:
        return code
    for lang in supported:
        entry = LANGUAGES.get(lang)
        if entry is not None and code in entry.aliases:
            return lang
    return code


def is_known_language(code: str | None) -> bool:
    return code is not None and code in LANGUAGES


def language_name(code: str) -> str:
    lang = LANGUAGES.get(code)
    return lang.name if lang else code


def deepgram_supports_multi(model: str | None, languages: list[str]) -> bool:
    """True when language=multi on this model can recognise every given language."""
    m = (model or "nova-3").lower()
    if not m.startswith(_DEEPGRAM_MULTI_MODELS):
        return False
    return all(LANGUAGES.get(code) is not None and LANGUAGES[code].deepgram_multi for code in languages)


# ── TTS engine language capability ──────────────────────────────────────────

# Documented per-model language coverage, restricted to registry languages.
_CARTESIA_MULTILINGUAL = frozenset({"en", "hi", "es", "fr", "de", "pt", "it", "ja", "zh"})
_ELEVENLABS_MULTILINGUAL = frozenset({"en", "hi", "es", "fr", "de", "pt", "it", "ja", "zh"})
# ElevenLabs models that speak English only.
_ELEVENLABS_ENGLISH_ONLY_MODELS = ("eleven_monolingual_v1", "eleven_turbo_v2", "eleven_flash_v2")
# The voice a Kokoro row without one speaks with (the provider factory uses this too), so the
# capability check and the runtime agree about a voiceless row.
KOKORO_DEFAULT_VOICE = "af_sarah"
# Kokoro voice ids start with their language's lang_code ('af_sarah' = a, 'hf_alpha' = h);
# 'b' is British English. Each voice is trained on that one language.
_KOKORO_VOICE_PREFIX_LANGUAGE = {
    **{l.kokoro_code: code for code, l in LANGUAGES.items() if l.kokoro_code},
    "b": "en",
}


def tts_languages(engine: str, model: str | None, voice: str | None = None) -> frozenset[str]:
    """Registry languages a TTS engine/model (and, for Kokoro, voice) can speak. Unknown
    engines: English only, so a new engine must be added here before a multilingual
    agent can use it."""
    engine = (engine or "").lower()
    if engine == "cartesia":
        m = (model or "sonic-2").lower()
        return frozenset({"en"}) if m.startswith("sonic-english") else _CARTESIA_MULTILINGUAL
    if engine == "elevenlabs":
        m = (model or "eleven_turbo_v2_5").lower()
        return frozenset({"en"}) if m in _ELEVENLABS_ENGLISH_ONLY_MODELS else _ELEVENLABS_MULTILINGUAL
    if engine == "kokoro":
        # A Kokoro voice speaks its own language only: an English voice reading Hindi
        # phonemes comes out barely intelligible.
        voice = voice or KOKORO_DEFAULT_VOICE
        voice_lang = _KOKORO_VOICE_PREFIX_LANGUAGE.get(voice[:1].lower())
        # A voice of a language we can't run (e.g. jf_alpha) speaks none of ours.
        return frozenset({voice_lang}) if voice_lang else frozenset()
    return frozenset({"en"})


def tts_model_of(engine: str, model: str | None, extra: dict | None) -> str | None:
    """The model id the TTS factory actually uses for this row."""
    extra = extra or {}
    if engine == "cartesia":
        return extra.get("model") or model
    if engine == "elevenlabs":
        return extra.get("model_id") or model
    return model


# ── Runtime resolution (shared by every IConfigProvider) ─────────────────────

@dataclass(frozen=True)
class ResolvedLanguages:
    stt_language:        str | None
    tts_language:        str | None
    default_language:    str | None
    supported_languages: tuple[str, ...]


def agent_supported_languages(agent_language: str | None, supported: Any) -> tuple[str, ...]:
    """Normalised, de-duplicated supported list with the default language first.
    Empty = single-language agent."""
    if not supported:
        return ()
    out: list[str] = []
    default = normalize_language(agent_language)
    for code in ([default] if default else []) + [normalize_language(c) for c in supported]:
        if code and code not in out:
            out.append(code)
    return tuple(out)


# Shape of a language tag ("en", "hi-IN", legacy "en_US"); anything else isn't sent to providers.
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")


def resolve_languages(agent: "Agent", stt: "ProviderConfig", tts: "ProviderConfig") -> ResolvedLanguages:
    """Effective per-role languages. Single-language agents: agent.language > the row's own
    language, per role (TTS never inherits the STT row's language). Multilingual agents:
    STT listens for every supported language (Deepgram multi / Whisper auto-detect)."""
    agent_language = agent.language
    if agent_language and not _LANGUAGE_TAG_RE.match(agent_language.strip()):
        # Free text saved before the Config Service checked it (e.g. "English"): providers
        # would reject it (Deepgram refuses the stream), so use the rows' own languages.
        log.warning(
            "agent=%s: language %r is not a language code — ignoring it; fix it in the agent's settings",
            agent.slug, agent_language,
        )
        agent_language = None
    supported = agent_supported_languages(agent_language, agent.supported_languages)
    tts_language = agent_language or tts.language
    if not supported:
        return ResolvedLanguages(agent_language or stt.language, tts_language, None, ())

    default = supported[0]
    if len(supported) == 1:
        stt_language: str | None = default
    elif stt.engine == "deepgram":
        if deepgram_supports_multi(stt.model, list(supported)):
            stt_language = DEEPGRAM_MULTI
        else:
            log.warning(
                "agent=%s: Deepgram model %r cannot code-switch across %s — listening in %s only",
                agent.slug, stt.model, ",".join(supported), default,
            )
            stt_language = default
    elif stt.engine == "faster_whisper":
        stt_language = None  # auto-detect per utterance
    else:
        stt_language = default
    return ResolvedLanguages(stt_language, tts_language or default, default, supported)


def same_tenant_tts_overrides(
    agent: "Agent", supported: tuple[str, ...], configs: dict[str, "ProviderConfig | None"],
) -> dict[str, "ProviderConfig"]:
    """Keep only overrides for supported languages whose row is a TTS config owned by the
    agent's tenant. Provider rows are fetched by id with no tenant scope, so this is the
    runtime half of the check the Config Service makes on write."""
    out: dict[str, ProviderConfig] = {}
    for lang, cfg in configs.items():
        if lang not in supported:
            continue
        if cfg is None:
            log.warning("agent=%s: TTS override for %s not found — using the base voice", agent.slug, lang)
            continue
        if cfg.role != "tts" or cfg.tenant_id is None or str(cfg.tenant_id) != str(agent.tenant_id):
            log.error(
                "agent=%s: TTS override for %s (provider_config %s) rejected — "
                "role=%s tenant=%s, expected a tts config of tenant %s",
                agent.slug, lang, cfg.id, cfg.role, cfg.tenant_id, agent.tenant_id,
            )
            continue
        # The Config Service checks this on every write, but a row edited before that check
        # existed (or outside it) must not voice the language with the wrong speaker.
        if lang not in tts_languages(cfg.engine, tts_model_of(cfg.engine, cfg.model, cfg.extra), cfg.voice):
            log.error(
                "agent=%s: TTS override for %s (provider_config %s, %s voice=%s) can't speak %s — "
                "using the base voice; fix the agent's Language & Voice settings",
                agent.slug, lang, cfg.id, cfg.engine, cfg.voice, language_name(lang),
            )
            continue
        out[lang] = cfg
    return out
