"""
ToolPolicyResolver._narrow tests — pure in-memory logic, no Postgres
needed (it never touches self._pool). Covers workflow per-node tool
scoping.

The auto-derived-companion tests that used to live here went away on
2026-09-18 with the mechanism itself: book_appointment silently granting
cancel_appointment/reschedule_appointment only made sense while those
were built-in tools sharing one Cal.com provider config. execute_api is
now the only DB-gated tool, and one custom API never implies another —
that relationship is an upstream edge in custom_api_params, resolved by
services/toolexec.
"""

from __future__ import annotations

from services.conversation.tools.policy_resolver import (
    ResolvedToolPolicy,
    ToolPolicyResolver,
    _narrow,
)
from services.conversation.tools.registry import ToolRegistry


def _policy(tool_name: str = "execute_api", tool_provider_config_id: str = "cfg1") -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve(tool_name)
    assert defn is not None, f"{tool_name!r} is not in the registry"
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id=tool_provider_config_id, engine="toolexec",
        api_key_ref=None, extra={}, timeout_ms=None, max_calls_per_turn=None,
    )


def _resolver() -> ToolPolicyResolver:
    return ToolPolicyResolver(pool=None, registry=ToolRegistry())


def test_only_none_leaves_the_agents_tools_alone():
    resolved = [_policy()]
    assert _narrow(resolved, None) == resolved


def test_a_node_can_narrow_the_agents_tools():
    resolved = [_policy()]
    assert _narrow(resolved, []) == []


def test_a_node_cannot_grant_a_tool_the_agent_does_not_have():
    # `only` subsets only — never grants a tool the agent lacks.
    assert _narrow([], ["execute_api"]) == []


def test_a_named_tool_survives_narrowing():
    resolved = [_policy()]
    assert {p.definition.name for p in _narrow(resolved, ["execute_api"])} == {"execute_api"}


def test_narrowing_no_longer_drags_companions_along():
    # The old _narrow expanded `only` with _AUTO_DERIVED_COMPANIONS before
    # filtering. Nothing expands it now: what you name is what you get.
    resolved = [_policy()]
    assert _narrow(resolved, ["some_other_tool"]) == []


async def test_enabled_tools_without_a_pool_resolves_nothing():
    # No Postgres configured (the YAML-fallback path) must degrade to "no
    # tools", never raise — search_knowledge is unaffected either way,
    # since it is a local tool that never comes through this resolver.
    resolver = _resolver()
    assert await resolver.enabled_tools("agent1", "tenant-a") == []
