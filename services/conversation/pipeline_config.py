"""
PipelineConfig — provider selection and parameters for the STT→LLM→TTS pipeline.

All fields have sensible defaults for a dev machine with Ollama running and the
FasterWhisper "small.en" model cached.  Every field can be overridden at startup
via environment variables — no source edits needed for different environments.

Environment-variable overrides (all optional):
  VOICEAI_STT_MODEL        — FasterWhisper model size (tiny/base/small/medium/large-v3,
                              or the .en-suffixed English-only variant of each —
                              skips language auto-detection entirely, eliminating
                              the class of bug where a short noise blip gets
                              transcribed as a wrong-language hallucination)
  VOICEAI_STT_DEVICE       — inference device: "cpu" | "cuda" | "auto"
  VOICEAI_STT_LANGUAGE     — ISO-639-1 code, e.g. "en"; empty string = auto-detect
  VOICEAI_LLM_MODEL        — Ollama model name, e.g. "llama3.2" or "mistral"
  VOICEAI_LLM_SYSTEM       — System prompt (replaces the default)
  VOICEAI_LLM_URL          — Ollama base URL (default http://localhost:11434)
  VOICEAI_LLM_TEMPERATURE  — Sampling temperature (float, e.g. "0.5")
  VOICEAI_LLM_TIMEOUT      — Per-token timeout in seconds (float)
  VOICEAI_TTS_ENGINE       — "macos" | "kokoro"
  VOICEAI_TTS_VOICE        — TTS voice name (macOS: "Samantha"; kokoro: "af_heart")
  VOICEAI_TTS_MACOS_WPM    — macOS words-per-minute (integer; 180 = natural rate)
  VOICEAI_TTS_KOKORO_SPEED — Kokoro speed multiplier (float; 1.0 = normal)
  VOICEAI_TTS_LANG_CODE    — Kokoro language code (default "a" = American English)
  VOICEAI_SENTIMENT_API_KEY  — API key for call-sentiment scoring; falls back to
                              OPENAI_API_KEY. Unset = scoring disabled (calls.sentiment
                              stays NULL, the Admin UI shows "not scored").
  VOICEAI_SENTIMENT_MODEL    — Scoring model (default "gpt-4o-mini")
  VOICEAI_SENTIMENT_URL      — Scoring API base URL (OpenAI-compatible)
  VOICEAI_SENTIMENT_TIMEOUT  — Per-score timeout in seconds (float)
  VOICEAI_SENTIMENT_MAX_TURNS— Max transcript turns sent per score (int)
  VOICEAI_SENTIMENT_DISABLED — Set to any non-empty value to turn scoring off
  POSTGRES_DSN             — Postgres DSN for call/transcript persistence;
                              unset = persistence disabled (TranscriptBuilder no-ops).
                              Same variable name and DSN services/config (FastAPI)
                              connects with — one Postgres, one env var, not two
                              per-service names for the same connection string.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str) -> str | None:
    val = os.environ.get(key)
    return val if val else None   # empty string → None


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val else default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val else default


@dataclass
class SttConfig:
    model_size:   str        = "small.en"  # "tiny"|"base"|"small"|"medium"|"large-v3", or ".en" variant
    device:       str        = "cpu"     # "cpu"|"cuda"|"auto"
    compute_type: str        = "int8"    # "int8" (CPU), "float16" (GPU)
    language:     str | None = "en"      # None = auto-detect

    def __post_init__(self) -> None:
        if v := _env("VOICEAI_STT_MODEL"):  self.model_size = v
        if v := _env("VOICEAI_STT_DEVICE"): self.device = v
        # Treat VOICEAI_STT_LANGUAGE="" as auto-detect (None)
        if "VOICEAI_STT_LANGUAGE" in os.environ:
            self.language = os.environ["VOICEAI_STT_LANGUAGE"] or None


@dataclass
class LlmConfig:
    model:       str   = "qwen2.5:7b"
    system:      str   = ("You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.")
    temperature: float = 0.7
    base_url:    str   = "http://localhost:11434"
    timeout_s:   float = 30.0

    def __post_init__(self) -> None:
        if v := _env("VOICEAI_LLM_MODEL"):  self.model    = v
        if v := _env("VOICEAI_LLM_SYSTEM"): self.system   = v
        if v := _env("VOICEAI_LLM_URL"):    self.base_url = v
        self.temperature = _env_float("VOICEAI_LLM_TEMPERATURE", self.temperature)
        self.timeout_s   = _env_float("VOICEAI_LLM_TIMEOUT",     self.timeout_s)


@dataclass
class TtsConfig:
    engine:       str   = "macos"      # "macos" (built-in) | "kokoro" (needs kokoro pkg)
    voice:        str   = "Samantha"   # macOS voice or kokoro voice identifier
    macos_wpm:    int   = 180          # macOS TTS words-per-minute (180 = natural rate)
    kokoro_speed: float = 1.0          # Kokoro speed multiplier (1.0 = normal)
    lang_code:    str   = "a"          # Kokoro only: 'a' = American English

    def __post_init__(self) -> None:
        if v := _env("VOICEAI_TTS_ENGINE"):     self.engine = v
        if v := _env("VOICEAI_TTS_VOICE"):      self.voice  = v
        if v := _env("VOICEAI_TTS_LANG_CODE"):  self.lang_code = v
        self.macos_wpm    = _env_int  ("VOICEAI_TTS_MACOS_WPM",    self.macos_wpm)
        self.kokoro_speed = _env_float("VOICEAI_TTS_KOKORO_SPEED",  self.kokoro_speed)


@dataclass
class SentimentConfig:
    """Call-sentiment scoring at end_call (see sentiment.py).

    Deliberately NOT the conversational LlmConfig above. Scoring is a
    classification over a finished transcript, not a voice turn, so it has
    different requirements: it is off the latency path (a 30s ceiling is
    fine where a live turn needs 300ms), it wants temperature 0, and it
    needs a model that can actually follow an output contract.

    Measured 2026-09-20 against the default local llama3.2:3b on six real
    transcripts: the LABEL was usually right, but the one-line reason was
    confidently fabricated — on a ThinkPad/MacBook pricing call it reported
    a caller "asking for order status despite the agent asking for email
    address", deterministically, on every rerun. A plausible-looking
    explanation of a conversation that did not happen is worse next to a
    call record than no explanation, so scoring defaults to a hosted model
    and stays OFF when no key is configured rather than falling back to a
    local model that invents its evidence.
    """
    api_key:     str | None = None    # None = scoring disabled entirely
    model:       str        = "gpt-4o-mini"
    base_url:    str        = "https://api.openai.com"
    timeout_s:   float      = 30.0
    max_turns:   int        = 40

    def __post_init__(self) -> None:
        # VOICEAI_SENTIMENT_API_KEY first so sentiment scoring can be pointed
        # at a different account/provider from anything else, but fall back to
        # the ordinary OPENAI_API_KEY so a deployment that already has one
        # gets this working without new configuration.
        self.api_key = _env("VOICEAI_SENTIMENT_API_KEY") or _env("OPENAI_API_KEY")
        if v := _env("VOICEAI_SENTIMENT_MODEL"):    self.model    = v
        if v := _env("VOICEAI_SENTIMENT_URL"):      self.base_url = v
        self.timeout_s = _env_float("VOICEAI_SENTIMENT_TIMEOUT",   self.timeout_s)
        self.max_turns = _env_int  ("VOICEAI_SENTIMENT_MAX_TURNS", self.max_turns)
        if _env("VOICEAI_SENTIMENT_DISABLED"):
            self.api_key = None


@dataclass
class DbConfig:
    database_url: str | None = None   # None = persistence disabled

    def __post_init__(self) -> None:
        self.database_url = _env("POSTGRES_DSN")


@dataclass
class PipelineConfig:
    stt:         SttConfig = field(default_factory=SttConfig)
    llm:         LlmConfig = field(default_factory=LlmConfig)
    tts:         TtsConfig = field(default_factory=TtsConfig)
    db:          DbConfig  = field(default_factory=DbConfig)
    sentiment:   SentimentConfig = field(default_factory=SentimentConfig)
    sample_rate: int       = 16_000   # output PCM sample rate (must match gateway)
    max_history: int       = 10       # conversation turns to keep in LLM context
