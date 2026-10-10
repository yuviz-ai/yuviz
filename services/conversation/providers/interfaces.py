"""
Provider interfaces for the STT → LLM → TTS pipeline.

All providers are async and designed for streaming where possible:
  ISTT  — batch transcription (receives accumulated PCM, returns transcript)
  ILLM  — streaming text generation (yields tokens)
  ITTS  — batch synthesis per sentence (receives text, returns PCM bytes)

Language (multilingual agents): providers that set `accepts_language = True` take an
optional keyword-only `language` on every call. Omitted (INSTANCE_LANGUAGE) means the
provider row's own language, so callers and implementations that predate it are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol


class _InstanceLanguage:
    def __repr__(self) -> str:
        return "INSTANCE_LANGUAGE"


# Default for the `language` keyword: "use the language the provider was built with".
# Distinct from None, which means auto-detect (STT) / provider default (TTS).
INSTANCE_LANGUAGE: Any = _InstanceLanguage()


@dataclass
class SttResult:
    text:       str
    confidence: float = 1.0
    # Detected spoken language (ISO 639-1), only when the provider actually detected it
    # (Whisper auto-detect, Deepgram language=multi); None when the language was forced.
    language:            str | None = None
    language_confidence: float | None = None
    # Share of words per language, when the provider tags words (Deepgram multi).
    language_shares:     dict[str, float] | None = None


@dataclass
class ChatMessage:
    role:    str   # "system" | "user" | "assistant" | "tool"
    content: str
    # Assistant message's tool calls: {"id", "name", "arguments": dict} each.
    tool_calls: list[dict[str, Any]] | None = field(default=None)
    # On a "tool"-role message: the tool_calls id this result answers.
    # Only generate_with_tools() reads these; each provider maps them to its wire shape.
    tool_call_id: str | None = field(default=None)


class ISTT(Protocol):
    """Transcribe accumulated PCM audio to text.

    Implementations with `accepts_language = True` also take `*, language=` on
    transcribe/feed_stream/finalize_stream (see module docstring)."""

    async def transcribe(self, audio: bytes, sample_rate: int) -> SttResult:
        """
        audio       — raw L16 PCM bytes
        sample_rate — samples per second (e.g. 16000)
        Returns SttResult with text="" when nothing was recognised.
        """
        ...

    async def feed_stream(self, session_id: str, chunk: bytes, sample_rate: int) -> None:
        """Forward one audio chunk as it arrives, before speech_ended.

        No-op for providers without a live-streaming API (e.g. FasterWhisperSTT)."""
        ...

    async def finalize_stream(self, session_id: str, audio: bytes, sample_rate: int) -> SttResult:
        """Called on speech_ended with the full buffer; streaming providers may ignore `audio`."""
        ...

    async def cancel_stream(self, session_id: str) -> None:
        """Release per-session streaming state when a session ends without finalize_stream."""
        ...


class ILLM(Protocol):
    """Generate a streaming text response from a conversation history."""

    def generate(
        self, messages: list[ChatMessage]
    ) -> AsyncGenerator[str, None]:
        """Yield text tokens as they are produced."""
        ...


class ITTS(Protocol):
    """Synthesise a text string to raw L16 PCM bytes at a given sample rate.

    Implementations with `accepts_language = True` also take `*, language=` on
    synthesize/synthesize_stream, so one cached instance can speak each call's language."""

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        """Returns raw L16 PCM at sample_rate Hz, or empty bytes on error."""
        ...

    def synthesize_stream(self, text: str, sample_rate: int) -> AsyncGenerator[bytes, None]:
        """Yield raw L16 PCM chunks at sample_rate Hz as they become available.

        Non-streaming providers yield their single synthesize() result once."""
        ...
