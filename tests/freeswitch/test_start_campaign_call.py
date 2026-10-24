"""start_campaign_call.lua dials only from a consumed, self-consistent intent and never from INVITE headers."""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from lua_harness import SCRIPTS, requires_lua, run
from services.campaigns import dial_request

pytestmark = requires_lua

KEY = "7d0b4d0e-0f2c-4a9f-9a3e-5d2c1b0a9e8f_1"
SECRET = b"egress-test-secret"


def _intent(**override):
    fields = dict(key=KEY, tenant_id="0b6f1c8e-1111-4222-8333-444455556666", campaign_id="aaaaaaaa-0000-4000-8000-000000000001",
                  contact_id="bbbbbbbb-0000-4000-8000-000000000002", call_uuid="cccccccc-0000-4000-8000-000000000003",
                  callee="+919876543210", caller_id="+918065354620", tenant_slug="yuviz", agent_slug="sales", max_call_s=900,
                  egress_secret=SECRET)
    fields.update(override)
    return dial_request.build_intent(**fields)


def _run(intent_json, *, claim="OK", key=KEY, extra_vars=None):
    vars = {"sip_h_X-Idempotency-Key": key, "local_ip_v4": "10.0.0.5", **(extra_vars or {})}
    return run("start_campaign_call.lua", vars=vars, replies=[
        ("hiredis_raw default getdel", intent_json), ("hiredis_raw default set campaign:claim:", claim), ("bgapi", "+OK")])


def _commands(actions, prefix):
    return [text for kind, text in actions if kind == "API" and text.startswith(prefix)]


def _result(actions):
    return next(text.split("=", 1)[1] for kind, text in actions if kind == "SETVAR" and text.startswith("sip_rh_X-Campaign-Result="))


def _as_redis_would(intent) -> str:
    return json.dumps(asdict(intent))


def test_a_valid_intent_dials_once_from_the_intent_alone():
    intent = _intent()
    actions = _run(_as_redis_would(intent), extra_vars={
        "sip_h_X-Callee": "+10000000000", "sip_h_X-Caller-Id": "+10000000001", "sip_h_X-Agent-DID": "+10000000002"})
    assert _result(actions) == "accepted"
    assert ("EXEC", "respond 603 Decline") in actions
    (originate,) = _commands(actions, "bgapi originate")
    assert originate.endswith("}sofia/gateway/kamailio_egress/+919876543210 &lua(start_voice_ai.lua)")
    assert "execute_on_answer='sched_hangup +900 ALLOTTED_TIMEOUT'" in originate
    assert f"sip_h_X-Egress-Token={intent.egress_token}" in originate and "sip_cid_type=pid" in originate
    assert f"origination_uuid={intent.call_uuid}" in originate and "origination_caller_id_number=+918065354620" in originate
    assert "+10000000000" not in originate and "+10000000001" not in originate and "+10000000002" not in originate
    assert _commands(actions, "hiredis_raw default set campaign:claim:") == [
        f"hiredis_raw default set campaign:claim:{KEY} {intent.call_uuid}|10.0.0.5 NX EX 105"]


@pytest.mark.parametrize("raw", ["-ERR no reply", "-ERR hiredis_raw default getdel x Command not found!", "", "not json"])
def test_no_intent_or_a_redis_error_dials_nothing(raw):
    actions = _run(raw)
    assert _result(actions) == "rejected"
    assert not _commands(actions, "bgapi") and not _commands(actions, "hiredis_raw default set")


def test_the_getdel_is_the_first_and_only_read_of_the_intent():
    actions = _run(_as_redis_would(_intent()))
    assert _commands(actions, "hiredis_raw")[0] == f"hiredis_raw default getdel campaign:intent:{KEY}"


@pytest.mark.parametrize("key", ["", "not-a-key", KEY + "000", KEY.replace("_1", "_1234"), KEY.upper(), KEY + "; x"])
def test_a_malformed_key_never_reaches_redis(key):
    actions = _run(_as_redis_would(_intent()), key=key)
    assert _result(actions) == "rejected"
    assert not _commands(actions, "hiredis_raw")


@pytest.mark.parametrize("claim", ["-ERR no reply", ""])
def test_a_second_trigger_for_the_same_key_is_a_duplicate_and_dials_nothing(claim):
    actions = _run(_as_redis_would(_intent()), claim=claim)
    assert _result(actions) == "duplicate"
    assert not _commands(actions, "bgapi")


def test_a_failed_claim_is_rejected_not_taken_for_a_duplicate():
    actions = _run(_as_redis_would(_intent()), claim="-ERR NOPERM")
    assert _result(actions) == "rejected" and not _commands(actions, "bgapi")


def _tamper(intent, **changes):
    return json.dumps({**asdict(intent), **changes})


@pytest.mark.parametrize("field, value", [
    ("caller_id", "+911111111111"), ("tenant_id", "0b6f1c8e-1111-4222-8333-444455557777"),
    ("max_call_s", 7200), ("key", "11111111-2222-4333-8444-555555555555_1")])
def test_a_token_that_disagrees_with_the_intent_is_rejected(field, value):
    # The callee is in the MAC but not in the token text, so only Kamailio can catch a changed callee.
    intent = _intent()
    changes = {field: value}
    actions = _run(_tamper(intent, **changes))
    assert _result(actions) == "rejected"
    assert not _commands(actions, "bgapi") and not _commands(actions, "hiredis_raw default set")


def test_an_intent_whose_token_expiry_is_not_the_minted_one_is_rejected():
    intent = _intent()
    assert _result(_run(_tamper(intent, exp=intent.exp + 1))) == "rejected"


@pytest.mark.parametrize("max_call_s", [None, "5", 5, 12345, 0, "abc"])
def test_a_missing_or_malformed_time_limit_is_rejected(max_call_s):
    raw = asdict(_intent())
    if max_call_s is None:
        del raw["max_call_s"]
    else:
        raw["max_call_s"] = max_call_s
    actions = _run(json.dumps(raw))
    assert _result(actions) == "rejected" and not _commands(actions, "bgapi")


def test_an_expired_intent_is_rejected():
    assert _result(_run(_as_redis_would(_intent(now=int(time.time()) - 3600)))) == "rejected"


@pytest.mark.parametrize("field", ["callee", "caller_id", "tenant_slug", "agent_slug", "tenant_id", "call_uuid", "egress_token"])
def test_an_intent_missing_a_field_is_rejected(field):
    raw = asdict(_intent())
    del raw[field]
    actions = _run(json.dumps(raw))
    assert _result(actions) == "rejected" and not _commands(actions, "bgapi")


def test_a_hostile_value_cannot_break_out_of_the_originate_string():
    raw = asdict(_intent())
    raw["callee"] = "+919876543210}sofia/gateway/other/1 &park,{x=1"
    actions = _run(json.dumps(raw))
    assert _result(actions) == "rejected" and not _commands(actions, "bgapi")


def test_the_lua_grace_constant_is_the_one_campaigns_mints_with():
    lua = (SCRIPTS / "start_campaign_call.lua").read_text()
    assert int(re.search(r"EGRESS_GRACE_S = (\d+)", lua).group(1)) == dial_request.EGRESS_GRACE_S
