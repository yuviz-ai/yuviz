"""Golden frames for both serializers — freezes today's exact Vobiz wire
bytes across the extraction (see bridge.py:377,463-471 before the move)."""

from __future__ import annotations

import base64
import json

from libs.media_stream_sdk.serializers import CloudonixSerializer, VobizSerializer


def test_vobiz_play_frame_matches_today():
    ser = VobizSerializer()
    frame = ser.play_frame("stream-1", b"\x01\x02\x03")
    assert json.loads(frame) == {
        "event": "playAudio",
        "media": {
            "contentType": "audio/x-mulaw",
            "sampleRate": 8000,
            "payload": base64.b64encode(b"\x01\x02\x03").decode("ascii"),
        },
        "streamId": "stream-1",
    }


def test_vobiz_clear_playback_matches_today():
    ser = VobizSerializer()
    frame = ser.clear_playback("stream-1")
    assert json.loads(frame) == {"event": "clearAudio", "streamId": "stream-1"}


def test_vobiz_reads_start_media_dtmf():
    ser = VobizSerializer()
    assert ser.event_kind({"event": "start"}) == "start"
    assert ser.stream_id({"event": "start", "start": {"streamId": "abc"}}) == "abc"
    assert ser.media_payload({"event": "media", "media": {"payload": "xyz"}}) == "xyz"
    assert ser.dtmf_digit({"event": "dtmf", "dtmf": {"digit": "5"}}) == "5"


def test_cloudonix_classifies_twilio_shaped_events():
    ser = CloudonixSerializer()
    assert ser.event_kind({"event": "start"}) == "start"
    assert ser.event_kind({"event": "media"}) == "media"
    assert ser.event_kind({"event": "dtmf"}) == "dtmf"
    assert ser.event_kind({"event": "stop"}) == "stop"
    assert ser.event_kind({"event": "connected"}) == "other"
    assert ser.event_kind({"event": "mark"}) == "other"


def test_cloudonix_reads_stream_sid_media_dtmf():
    ser = CloudonixSerializer()
    assert ser.stream_id({"event": "start", "start": {"streamSid": "MZ123"}}) == "MZ123"
    assert ser.media_payload({"event": "media", "media": {"payload": "xyz"}}) == "xyz"
    assert ser.dtmf_digit({"event": "dtmf", "dtmf": {"digit": "5"}}) == "5"


def test_cloudonix_play_frame_and_clear():
    ser = CloudonixSerializer()
    frame = ser.play_frame("MZ123", b"\x01\x02\x03")
    assert json.loads(frame) == {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(b"\x01\x02\x03").decode("ascii")},
    }
    clear = ser.clear_playback("MZ123")
    assert json.loads(clear) == {"event": "clear", "streamSid": "MZ123"}
