"""A malformed control frame from the browser must be ignored, never kill the call (issue #184)."""

from __future__ import annotations

import asyncio
import json

import pytest

from services.webcall import __main__ as webcall
from services.webcall.tests.test_first_frame import CRED_FRAME, PLAIN_PATH, FakeWs, conv  # noqa: F401

GOOD = json.dumps({"type": "speech_ended", "duration_ms": 100, "energy_db": -20.0})

BAD_FRAMES = [
    json.dumps({"type": "speech_ended", "duration_ms": "abc"}),
    json.dumps({"type": "speech_ended", "duration_ms": 2**32}),
    json.dumps({"type": "speech_ended", "duration_ms": -1}),
    json.dumps({"type": "speech_ended", "duration_ms": 1e300}),
    json.dumps({"type": "speech_ended", "duration_ms": None}),
    json.dumps({"type": "speech_ended", "duration_ms": True}),
    json.dumps({"type": "speech_ended", "duration_ms": [1]}),
    '{"type":"speech_ended","duration_ms":NaN}',
    '{"type":"speech_ended","duration_ms":Infinity}',
    json.dumps({"type": "speech_ended", "energy_db": "loud"}),
    '{"type":"speech_ended","energy_db":NaN}',
    '{"type":"speech_ended","energy_db":1e999}',
    json.dumps([1, 2]),
    json.dumps("speech_ended"),
    "null",
    "42",
    "[" * 100000,
]


@pytest.mark.parametrize("frame", BAD_FRAMES, ids=[f"bad{i}" for i in range(len(BAD_FRAMES))])
def test_malformed_control_frame_is_ignored_and_call_survives(conv, frame):  # noqa: F811
    ws = FakeWs(PLAIN_PATH, [CRED_FRAME, frame, GOOD])
    asyncio.run(webcall._handle_connection(ws))

    kinds = [w.WhichOneof("payload") for w in conv.writes]
    assert kinds == ["session_open", "speech_ended"]  # bad frame dropped, later good frame still forwarded
    assert ws.closed_with is None or ws.closed_with[0] != 1011


def test_boundary_values_are_forwarded(conv):  # noqa: F811
    frames = [json.dumps({"type": "speech_ended", "duration_ms": v}) for v in (0, 2**32 - 1, 12.9)]
    ws = FakeWs(PLAIN_PATH, [CRED_FRAME, *frames])
    asyncio.run(webcall._handle_connection(ws))

    sent = [w.speech_ended.duration_ms for w in conv.writes if w.WhichOneof("payload") == "speech_ended"]
    assert sent == [0, 2**32 - 1, 12]
