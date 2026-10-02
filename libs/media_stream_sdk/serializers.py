"""Per-provider Media Streams wire shapes; a new provider is a new class here."""

from __future__ import annotations

import base64
import json
from typing import Protocol


class MediaStreamSerializer(Protocol):
    def event_kind(self, event: dict) -> str:
        """"start"|"media"|"dtmf"|"stop"|"other"."""
        ...

    def stream_id(self, event: dict) -> str | None:
        """From the start event."""
        ...

    def media_payload(self, event: dict) -> str | None:
        """Inbound base64 mulaw."""
        ...

    def dtmf_digit(self, event: dict) -> str | None:
        ...

    def play_frame(self, stream_id: str | None, ulaw_frame: bytes) -> str:
        """JSON text frame."""
        ...

    def clear_playback(self, stream_id: str | None) -> str | None:
        """JSON text, None = unsupported."""
        ...


class VobizSerializer:
    """Vobiz wire shapes; keep byte-for-byte stable."""

    def event_kind(self, event: dict) -> str:
        kind = event.get("event")
        if kind in ("start", "media", "dtmf", "stop"):
            return kind
        return "other"

    def stream_id(self, event: dict) -> str | None:
        return event.get("start", {}).get("streamId")

    def media_payload(self, event: dict) -> str | None:
        return event.get("media", {}).get("payload")

    def dtmf_digit(self, event: dict) -> str | None:
        return event.get("dtmf", {}).get("digit")

    def play_frame(self, stream_id: str | None, ulaw_frame: bytes) -> str:
        return json.dumps({
            "event": "playAudio",
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate": 8000,
                "payload": base64.b64encode(ulaw_frame).decode("ascii"),
            },
            "streamId": stream_id,
        })

    def clear_playback(self, stream_id: str | None) -> str | None:
        return json.dumps({"event": "clearAudio", "streamId": stream_id})


class CloudonixSerializer:
    """Twilio Media Streams-compatible — reads `start.streamSid`,
    `media.payload`, `dtmf.digit`; maps `connected`/`mark` to `"other"`."""

    def event_kind(self, event: dict) -> str:
        kind = event.get("event")
        if kind in ("start", "media", "dtmf", "stop"):
            return kind
        return "other"

    def stream_id(self, event: dict) -> str | None:
        return event.get("start", {}).get("streamSid")

    def media_payload(self, event: dict) -> str | None:
        return event.get("media", {}).get("payload")

    def dtmf_digit(self, event: dict) -> str | None:
        return event.get("dtmf", {}).get("digit")

    def play_frame(self, stream_id: str | None, ulaw_frame: bytes) -> str:
        return json.dumps({
            "event": "media",
            "streamSid": stream_id,
            "media": {"payload": base64.b64encode(ulaw_frame).decode("ascii")},
        })

    def clear_playback(self, stream_id: str | None) -> str | None:
        return json.dumps({"event": "clear", "streamSid": stream_id})
