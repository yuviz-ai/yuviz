"""The dtmf websocket branch — forwarding onto the single-writer gRPC queue
after any held audio is flushed, and never logging the digit value
(lesson 33)."""

from __future__ import annotations

import json
import re

from services.vobiz.bridge import VobizCallBridge


class _FakeWs:
    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames

    async def iter_text(self):
        for frame in self._frames:
            yield json.dumps(frame)


def _bridge() -> VobizCallBridge:
    return VobizCallBridge(
        call_uuid="call-1", tenant_slug="acme", agent_slug="ivr", direction="inbound",
    )


async def test_dtmf_event_enqueues_exactly_one_gateway_message_after_held_audio():
    bridge = _bridge()
    # Pre-roll audio held in the delay buffer, as it would be mid-call.
    held_msg = object()
    bridge._audio_delay_buf.append((0.0, held_msg))

    ws = _FakeWs([{"event": "dtmf", "dtmf": {"digit": "7"}}])
    await bridge._vobiz_to_grpc(ws, "session-1")

    queued = []
    while not bridge._grpc_write_queue.empty():
        queued.append(bridge._grpc_write_queue.get_nowait())

    assert queued[0] is held_msg  # flushed first
    assert len(queued) == 2
    assert queued[1].WhichOneof("payload") == "dtmf"
    assert queued[1].dtmf.digit == "7"
    assert queued[1].dtmf.session_id == "session-1"


async def test_missing_or_empty_digit_enqueues_nothing():
    bridge = _bridge()
    ws = _FakeWs([
        {"event": "dtmf", "dtmf": {}},
        {"event": "dtmf", "dtmf": {"digit": ""}},
    ])
    await bridge._vobiz_to_grpc(ws, "session-1")
    assert bridge._grpc_write_queue.empty()


async def test_dtmf_digit_is_never_logged(caplog):
    bridge = _bridge()
    ws = _FakeWs([{"event": "dtmf", "dtmf": {"digit": "7"}}])
    with caplog.at_level("INFO", logger="vobiz.bridge"):
        await bridge._vobiz_to_grpc(ws, "session-1")

    records = [r for r in caplog.records if r.name == "vobiz.bridge"]
    assert records, "expected the bridge to log something for the dtmf event"
    for record in records:
        assert "7" not in record.getMessage()


def test_no_digit_shaped_format_string_in_bridge_source():
    src = open("services/vobiz/bridge.py").read()
    assert not re.search(r"digit=%s", src)
