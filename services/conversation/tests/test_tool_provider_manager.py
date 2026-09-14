"""
ToolProviderManager tests — cal_com and twilio are now two fully
independent single-secret engines (no more secondary_api_key_ref), each
resolved from its own tool_provider_config.
"""

from __future__ import annotations

import pytest

from services.conversation.tools.policy_resolver import ResolvedToolPolicy
from services.conversation.tools.provider_manager import (
    ToolProviderManager, _make_cal_com, _make_toolexec, _make_twilio_sms,
)
from services.conversation.tools.registry import ToolRegistry


def _cal_com_policy(**extra_overrides) -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve("book_appointment")
    extra = {"event_type_id": 123, **extra_overrides}
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id="cfg1", engine="cal_com",
        api_key_ref="ref:cal", extra=extra, timeout_ms=None, max_calls_per_turn=None,
    )


def _toolexec_policy() -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve("execute_api")
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id="cfg3", engine="toolexec",
        api_key_ref=None, extra={}, timeout_ms=None, max_calls_per_turn=None,
    )


def _twilio_policy(**extra_overrides) -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve("send_sms")
    extra = {"account_sid": "ACtest", "from_number": "+19998887777", **extra_overrides}
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id="cfg2", engine="twilio",
        api_key_ref="ref:twilio", extra=extra, timeout_ms=None, max_calls_per_turn=None,
    )


class _FakeSecretResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def resolve(self, ref: str) -> str:
        return self._values[ref]


async def test_get_constructs_cal_com_provider():
    manager = ToolProviderManager(_FakeSecretResolver({"ref:cal": "cal-api-key"}))
    provider = await manager.get(_cal_com_policy())
    assert provider is not None


async def test_get_constructs_twilio_provider():
    manager = ToolProviderManager(_FakeSecretResolver({"ref:twilio": "twilio-auth-token"}))
    provider = await manager.get(_twilio_policy())
    assert provider is not None


async def test_make_cal_com_requires_api_key():
    with pytest.raises(ValueError, match="api_key_ref"):
        await _make_cal_com(_cal_com_policy(), None)


async def test_make_cal_com_requires_event_type_id():
    policy = ResolvedToolPolicy(
        definition=ToolRegistry().resolve("book_appointment"), tool_provider_config_id="cfg1",
        engine="cal_com", api_key_ref="ref:cal", extra={}, timeout_ms=None, max_calls_per_turn=None,
    )
    with pytest.raises(ValueError, match="event_type_id"):
        await _make_cal_com(policy, "cal-api-key")


async def test_make_twilio_sms_requires_api_key():
    with pytest.raises(ValueError, match="api_key_ref"):
        await _make_twilio_sms(_twilio_policy(), None)


async def test_make_twilio_sms_requires_account_sid_and_from_number():
    policy = _twilio_policy(account_sid="", from_number="")
    with pytest.raises(ValueError, match="account_sid"):
        await _make_twilio_sms(policy, "twilio-auth-token")


async def test_make_twilio_sms_constructs_when_fully_configured():
    provider = await _make_twilio_sms(_twilio_policy(), "twilio-auth-token")
    assert provider is not None


async def test_get_constructs_toolexec_provider_with_no_api_key_ref():
    # engine='toolexec' is internal infrastructure — no api_key_ref, no
    # secret to resolve. The manager must still hand back a real provider.
    manager = ToolProviderManager(_FakeSecretResolver({}))
    provider = await manager.get(_toolexec_policy())
    assert provider is not None


async def test_make_toolexec_requires_no_api_key():
    provider = await _make_toolexec(_toolexec_policy(), None)
    assert provider is not None
