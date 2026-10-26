from __future__ import annotations

import pytest
from pydantic import ValidationError

from libs.config_sdk.dial_targets import is_dial_number, is_transfer_destination
from services.config.schemas import AgentUpdate

# Same shapes as tests/esl_client_test.cpp's kInjectedDestinations: the Gateway
# refuses these as a final check, so every earlier layer must refuse them too.
INJECTED = [
    "1001\n\napi hupall",
    "1001\r\n\r\napi hupall",
    "1001\n",
    "1001 XML public",
    "sip:agent@example.com' inline\n\napi hupall",
    "sip:agent@example.com\n\napi hupall",
    "{sip_h_X-Yuviz-Leg=transfer}sip:1002@example.com",
    "sip:a@b,sofia/external/sip:c@d",
    "sip:${global_getvar(x)}@example.com",
    "sip:a@b@c",
    "1",
    "1234567890123456",
    "１００１",
]


@pytest.mark.parametrize("value", INJECTED)
def test_transfer_destination_rejects_injection_shapes(value):
    assert not is_transfer_destination(value)


@pytest.mark.parametrize("value", [
    "1001", "+18005550100", "sip:agent@example.com", "sips:agent@example.com",
    "sip:agent@pbx.example.com:5070;transport=tcp",
])
def test_transfer_destination_accepts_real_shapes(value):
    assert is_transfer_destination(value)


# The platform's own AI numbers loop back into the platform (Kamailio refuses
# them from FreeSWITCH), and a URI at this host enters FreeSWITCH's dialplan.
PLATFORM = [
    "788", "5000", "5005", "5009",
    "sip:3500@127.0.0.1:5080", "sip:3500@127.1", "sip:779@2130706433", "sip:x@0.0.0.0",
    "sip:x@localhost", "sip:x@LOCALHOST.", "sip:x@fs.localhost:5080",
    "sip:x@pbx.example.com;maddr=127.0.0.1", "sip:x@:5080",
]


@pytest.mark.parametrize("value", PLATFORM)
def test_transfer_destination_rejects_platform_targets(value):
    assert not is_transfer_destination(value)


@pytest.mark.parametrize("value", ["15005", "+5005", "50010", "+14155005555", "7880", "sip:788@example.com"])
def test_transfer_destination_accepts_numbers_that_only_contain_a_platform_number(value):
    assert is_transfer_destination(value)


@pytest.mark.parametrize("value", PLATFORM)
def test_agent_update_rejects_platform_transfer_destination(value):
    with pytest.raises(ValidationError):
        AgentUpdate(transfer_destination=value)


@pytest.mark.parametrize("value", ["+15551234567}\n\napi hupall", "+1555,x=1", "anonymous", "+1 555"])
def test_dial_number_rejects_non_numbers(value):
    assert not is_dial_number(value)


@pytest.mark.parametrize("field", ["platform_did", "custom_caller_id"])
@pytest.mark.parametrize("value", ["+15551234567}\n\napi hupall", "+1555,x=1", "anonymous"])
def test_agent_update_rejects_bad_caller_id(field, value):
    with pytest.raises(ValidationError):
        AgentUpdate(**{field: value})


@pytest.mark.parametrize("value", INJECTED)
def test_agent_update_rejects_bad_transfer_destination(value):
    with pytest.raises(ValidationError):
        AgentUpdate(transfer_destination=value)


def test_agent_update_accepts_real_values_and_clearing():
    AgentUpdate(transfer_destination="sip:agent@example.com", platform_did="+18005550100", custom_caller_id="1001")
    AgentUpdate(transfer_destination="", platform_did=None, custom_caller_id="")
