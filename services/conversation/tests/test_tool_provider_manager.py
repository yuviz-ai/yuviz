"""
ToolProviderManager tests.

Only one engine remains (2026-09-18): 'toolexec'. The cal_com and twilio
engines were removed with the calendar/SMS built-ins, and search_knowledge
— the agent's other tool — is an in-process local tool that never reaches
this manager at all (see registry.py's SEARCH_KNOWLEDGE).
"""

from __future__ import annotations

import pytest

from services.conversation.tools.policy_resolver import ResolvedToolPolicy
from services.conversation.tools.provider_manager import ToolProviderManager, _make_toolexec
from services.conversation.tools.registry import ToolRegistry


def _toolexec_policy(config_id: str = "cfg3") -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve("execute_api")
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id=config_id, engine="toolexec",
        api_key_ref=None, extra={}, timeout_ms=None, max_calls_per_turn=None,
    )


class _FakeSecretResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def resolve(self, ref: str) -> str:
        return self._values[ref]


async def test_get_constructs_toolexec_provider_with_no_api_key_ref():
    # engine='toolexec' is internal infrastructure — no api_key_ref, no
    # secret to resolve. The manager must still hand back a real provider.
    manager = ToolProviderManager(_FakeSecretResolver({}))
    provider = await manager.get(_toolexec_policy())
    assert provider is not None


async def test_make_toolexec_requires_no_api_key():
    provider = await _make_toolexec(_toolexec_policy(), None)
    assert provider is not None


async def test_provider_is_cached_by_tool_provider_config_id():
    manager = ToolProviderManager(_FakeSecretResolver({}))
    first = await manager.get(_toolexec_policy("cfg-a"))
    again = await manager.get(_toolexec_policy("cfg-a"))
    other = await manager.get(_toolexec_policy("cfg-b"))
    assert first is again
    assert first is not other
    assert manager.cached_ids() == frozenset({"cfg-a", "cfg-b"})


async def test_unknown_engine_is_rejected():
    manager = ToolProviderManager(_FakeSecretResolver({}))
    policy = ResolvedToolPolicy(
        definition=ToolRegistry().resolve("execute_api"), tool_provider_config_id="cfg9",
        engine="cal_com",  # removed engine — must not silently resolve to anything
        api_key_ref=None, extra={}, timeout_ms=None, max_calls_per_turn=None,
    )
    with pytest.raises(ValueError, match="no tool provider factory"):
        await manager.get(policy)
