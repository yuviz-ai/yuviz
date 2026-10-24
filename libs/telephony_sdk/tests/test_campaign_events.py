from __future__ import annotations

import pytest

from libs.telephony_sdk.campaign_events import (
    GROUP, STREAM_CONV, STREAM_CPAAS, STREAM_FS, CampaignCallEvent, decode, encode, source_for_stream,
)


def _wire(fields: dict[str, str]) -> dict[bytes, bytes]:
    return {k.encode(): v.encode() for k, v in fields.items()}


def test_streams_and_group_are_distinct_names():
    assert len({STREAM_FS, STREAM_CPAAS, STREAM_CONV}) == 3
    assert GROUP


@pytest.mark.parametrize("source, kind, answered, billable_ms", [
    ("fs", "answered", True, None),
    ("fs", "hangup", True, 61234),
    ("fs", "hangup", False, None),
    ("cpaas", "answered", True, None),
    ("cpaas", "hangup", False, None),
])
def test_round_trip_call_events(source, kind, answered, billable_ms):
    fields = encode(
        source=source, kind=kind, key="abc_1", tenant_id="t-1", provider_call_id="pc-1",
        call_session_id="s-1", cause="NORMAL_CLEARING", answered=answered, billable_ms=billable_ms,
    )
    assert decode(_wire(fields)) == CampaignCallEvent(
        kind=kind, key="abc_1", tenant_id="t-1", provider_call_id="pc-1", call_session_id="s-1",
        cause="NORMAL_CLEARING", answered=answered, billable_ms=billable_ms,
    )


def test_round_trip_dnc_request():
    fields = encode(
        source="conv", kind="dnc_request", key=None, tenant_id=None, provider_call_id=None,
        call_session_id="s-9", cause="", answered=False, billable_ms=None,
    )
    event = decode(_wire(fields))
    assert (event.kind, event.call_session_id, event.key, event.tenant_id) == ("dnc_request", "s-9", None, None)


def test_decode_rejects_unknown_kind():
    with pytest.raises(ValueError):
        decode(_wire({"kind": "transfer", "key": "k"}))
    with pytest.raises(ValueError):
        decode(_wire({"key": "k"}))


def test_source_comes_from_the_stream_not_the_entry():
    assert source_for_stream(STREAM_FS) == "fs"
    assert source_for_stream(STREAM_CPAAS) == "cpaas"
    assert source_for_stream(STREAM_CONV) is None
    assert source_for_stream("campaign:events:other") is None

    forged = encode(
        source="fs", kind="hangup", key="k_1", tenant_id="t-1", provider_call_id=None,
        call_session_id=None, cause="x", answered=True, billable_ms=999999999,
    )
    assert forged["source"] == "fs"
    assert source_for_stream(STREAM_CPAAS) == "cpaas"
    assert not hasattr(decode(_wire(forged)), "source")
