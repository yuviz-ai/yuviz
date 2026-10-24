"""campaign_event.lua reports campaign legs only, in the entry shape Campaigns' consumer decodes."""
from __future__ import annotations

import re

from lua_harness import SCRIPTS, requires_lua, run

from libs.telephony_sdk import campaign_events

pytestmark = requires_lua

ANSWER = {"Event-Name": "CHANNEL_ANSWER", "variable_campaign_key": "7d0b4d0e-0f2c-4a9f-9a3e-5d2c1b0a9e8f_1",
          "variable_campaign_tenant_id": "0b6f1c8e-1111-4222-8333-444455556666",
          "Unique-ID": "cccccccc-0000-4000-8000-000000000003"}
HANGUP = {**ANSWER, "Event-Name": "CHANNEL_HANGUP_COMPLETE", "Hangup-Cause": "NORMAL_CLEARING",
          "variable_answer_epoch": "1792586223", "variable_billmsec": "3040"}


def _xadd(headers) -> dict[str, str]:
    actions = run("campaign_event.lua", headers=headers, replies=[("hiredis_raw default xadd", "1792586276490-0")])
    commands = [text for kind, text in actions if kind == "API"]
    if not commands:
        return {}
    (command,) = commands
    prefix = "hiredis_raw default xadd campaign:events:fs MAXLEN ~ 100000 * "
    assert command.startswith(prefix)
    pairs = command[len(prefix):].split(" ")
    return dict(zip(pairs[::2], pairs[1::2]))


def test_the_lua_field_names_are_exactly_the_ones_encode_writes_for_a_freeswitch_event():
    lua = (SCRIPTS / "campaign_event.lua").read_text()
    names = re.search(r"local FIELDS = \{(.*?)\}", lua).group(1)
    in_lua = re.findall(r'"(\w+)"', names)
    full = campaign_events.encode(source="fs", kind="hangup", key="k", tenant_id="t", provider_call_id="p",
                                  call_session_id="s", cause="c", answered=True, billable_ms=1)
    assert in_lua and set(in_lua) == set(full) - {"provider_call_id"}


def test_an_answered_event_carries_the_tenant_from_the_channel_variable():
    entry = _xadd(ANSWER)
    assert entry == {"source": "fs", "kind": "answered", "key": ANSWER["variable_campaign_key"],
                     "tenant_id": ANSWER["variable_campaign_tenant_id"], "call_session_id": ANSWER["Unique-ID"],
                     "answered": "1"}


def test_a_hangup_event_carries_tenant_cause_and_billable_ms():
    entry = _xadd(HANGUP)
    assert entry["kind"] == "hangup" and entry["tenant_id"] == HANGUP["variable_campaign_tenant_id"]
    assert entry["cause"] == "NORMAL_CLEARING" and entry["billable_ms"] == "3040" and entry["answered"] == "1"


def test_the_entry_decodes_with_the_consumer_schema():
    entry = _xadd(HANGUP)
    event = campaign_events.decode({k.encode(): v.encode() for k, v in entry.items()})
    assert (event.kind, event.key, event.tenant_id, event.answered, event.billable_ms) == (
        "hangup", HANGUP["variable_campaign_key"], HANGUP["variable_campaign_tenant_id"], True, 3040)


def test_answered_is_true_only_when_answer_epoch_is_positive():
    for epoch in (None, "0", "", "garbage"):
        assert _xadd({**HANGUP, "variable_answer_epoch": epoch})["answered"] == "0", epoch
    assert _xadd({**HANGUP, "variable_answer_epoch": "1"})["answered"] == "1"


def test_a_channel_without_a_campaign_key_is_ignored():
    headers = {k: v for k, v in HANGUP.items() if k != "variable_campaign_key"}
    assert _xadd(headers) == {}


def test_a_hostile_header_value_cannot_add_stream_fields():
    entry = _xadd({**HANGUP, "variable_campaign_tenant_id": "t billable_ms 99999999 kind answered"})
    assert entry["billable_ms"] == "3040" and entry["kind"] == "hangup"
