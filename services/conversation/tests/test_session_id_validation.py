"""Converse refuses an unusable session_id with INVALID_ARGUMENT before keying any state by it (#211)."""
from __future__ import annotations

import asyncio
import os

import grpc
import pytest

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")

from services.conversation.tests.test_session_id_collision import _Stream, _servicer  # noqa: E402

TENANT = "validation-tenant"

_BAD_IDS = [
    "", " ", "   ", "\t\n", " ",            # empty / whitespace-only
    "a\x00b", "\x00",                            # NUL
    "line\nbreak", "cr\rlf", "tab\there", "\x7f", "esc\x1b[31m",  # control chars (log forging)
    "a b",                                  # unicode line separator
    "x" * 129, "x" * 100_000,                    # too long
]


@pytest.mark.parametrize("bad", _BAD_IDS, ids=lambda b: repr(b[:12]) + f"/{len(b)}")
async def test_invalid_session_id_is_refused_before_any_state(bad):
    servicer, factory_calls = _servicer()
    stream = _Stream(servicer, bad, TENANT)
    await asyncio.wait_for(stream.task, 2)

    stream.context.abort.assert_awaited_once()
    code, message = stream.context.abort.await_args.args
    assert code == grpc.StatusCode.INVALID_ARGUMENT
    assert len(message) < 64  # never echoes the id back
    assert not factory_calls  # no handler / call row for it
    assert not servicer._live_sessions  # nothing left claimed


@pytest.mark.parametrize(
    "good",
    ["x", "x" * 128, "call-123_abc.def:9", "9f1c2e4a-1111-2222-3333-444455556666", "é-ok", "has space"],
)
async def test_valid_session_ids_are_still_accepted(good):
    servicer, factory_calls = _servicer()
    stream = _Stream(servicer, good, TENANT)
    await asyncio.wait_for(stream.ready.wait(), 2)
    stream.context.abort.assert_not_awaited()
    await stream.hang_up()
    assert factory_calls[0].session_id == good


async def test_refused_empty_id_does_not_block_a_later_valid_stream():
    servicer, factory_calls = _servicer()
    bad = _Stream(servicer, "", "tenant-a")
    await asyncio.wait_for(bad.task, 2)
    ok = _Stream(servicer, "real-id", "tenant-b")
    await asyncio.wait_for(ok.ready.wait(), 2)
    await ok.hang_up()
    assert [c.tenant_id for c in factory_calls] == ["tenant-b"]
