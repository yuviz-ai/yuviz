"""
Provider interfaces for the STT → LLM → TTS pipeline.

All providers are async and designed for streaming where possible:
  ISTT  — batch transcription (receives accumulated PCM, returns transcript)
  ILLM  — streaming text generation (yields tokens)
  ITTS  — batch synthesis per sentence (receives text, returns PCM bytes)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol


@dataclass
class SttResult:
    text:       str
    confidence: float = 1.0


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
    """Transcribe accumulated PCM audio to text."""

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
    """Synthesise a text string to raw L16 PCM bytes at a given sample rate."""

    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        """Returns raw L16 PCM at sample_rate Hz, or empty bytes on error."""
        ...

    def synthesize_stream(self, text: str, sample_rate: int) -> AsyncGenerator[bytes, None]:
        """Yield raw L16 PCM chunks at sample_rate Hz as they become available.

        Non-streaming providers yield their single synthesize() result once."""
        ...
