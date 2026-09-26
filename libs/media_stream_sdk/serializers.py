"""
The one seam that knows event names, `streamId`/`streamSid`, or frame
JSON shapes for a Media Streams-style provider. `MediaStreamBridge`
(bridge.py) never touches raw JSON keys itself — it calls through a
`MediaStreamSerializer` so a new provider is a new class here and
nothing else.

`VobizSerializer` freezes today's exact Vobiz wire bytes
(services/vobiz/bridge.py before the extraction). `CloudonixSerializer`
speaks Twilio Media Streams' shape, which Cloudonix's wire protocol
clones (see .sdlc/cloudonix-telephony-provider/02-design.md's Approach).
Field lookups are case-tolerant, mirroring services/vobiz/app.py:139's
`form.get("CallUUID") or form.get("call_uuid")` habit — exact casing is
unverified until the trial call (OQ2/OQ3).
"""

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
    """Exactly today's Vobiz literals — see bridge.py's module docstring
    for why pacing/barge-in depend on these shapes staying byte-for-byte
    identical."""

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
