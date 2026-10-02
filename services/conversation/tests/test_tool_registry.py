"""ToolRegistry tests: only execute_api resolves; search_knowledge is a local tool supplied by pipeline.py."""

from __future__ import annotations

from services.conversation.tools.registry import SEARCH_KNOWLEDGE, ToolRegistry
from services.conversation.tools.types import ToolDefinition


def test_default_registry_holds_execute_api_only():
    # The guard against the old shape coming back: one first-class tool
    # per capability is exactly what this registry no longer is.
    reg = ToolRegistry()
    assert {d.name for d in reg.all()} == {"execute_api"}


def test_default_registry_has_execute_api():
    reg = ToolRegistry()
    defn = reg.resolve("execute_api")

    assert defn is not None
    assert defn.category == "custom_api"
    assert defn.parameters_schema["required"] == ["api_name"]
    # The enum is empty in code on purpose — ToolPolicyResolver fills it
    # per agent at resolve time from that agent's own enabled APIs.
    assert defn.parameters_schema["properties"]["api_name"]["enum"] == []


def test_calendar_and_sms_builtins_are_gone():
    reg = ToolRegistry()
    for removed in ("book_appointment", "cancel_appointment", "reschedule_appointment", "send_sms"):
        assert reg.resolve(removed) is None


def test_search_knowledge_is_not_db_gated():
    # Must not resolve: nothing backs it, and an agent_tool_policies row for it would silently do nothing.
    reg = ToolRegistry()
    assert reg.resolve("search_knowledge") is None
    assert SEARCH_KNOWLEDGE.name == "search_knowledge"
    assert SEARCH_KNOWLEDGE.parameters_schema["required"] == ["query"]
    assert SEARCH_KNOWLEDGE.category == "knowledge"


def test_resolve_unknown_tool_returns_none():
    reg = ToolRegistry()
    assert reg.resolve("send_email") is None


def test_register_adds_a_new_tool_without_touching_defaults():
    reg = ToolRegistry()
    custom = ToolDefinition(name="custom_tool", description="d", parameters_schema={})
    reg.register(custom)

    assert reg.resolve("custom_tool") is custom
    assert reg.resolve("execute_api") is not None  # still there


def test_to_generic_schema_shape():
    reg = ToolRegistry()
    defn = reg.resolve("execute_api")
    schema = defn.to_generic_schema()

    assert schema["name"] == "execute_api"
    assert schema["parameters"] is defn.parameters_schema
