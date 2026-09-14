from __future__ import annotations

from services.conversation.tools.registry import ToolRegistry
from services.conversation.tools.types import ToolDefinition


def test_default_registry_has_book_appointment():
    reg = ToolRegistry()
    defn = reg.resolve("book_appointment")

    assert defn is not None
    assert defn.category == "calendar"
    assert "requested_datetime" in defn.parameters_schema["properties"]
    assert defn.parameters_schema["required"] == ["requested_datetime"]
    # event_type is deliberately never an LLM-facing parameter (v1 scope —
    # one event type per agent, configured, not chosen by the model).
    assert "event_type" not in defn.parameters_schema["properties"]


def test_resolve_unknown_tool_returns_none():
    reg = ToolRegistry()
    assert reg.resolve("send_email") is None


def test_register_adds_a_new_tool_without_touching_defaults():
    reg = ToolRegistry()
    custom = ToolDefinition(name="custom_tool", description="d", parameters_schema={})
    reg.register(custom)

    assert reg.resolve("custom_tool") is custom
    assert reg.resolve("book_appointment") is not None  # still there


def test_all_returns_every_registered_definition():
    reg = ToolRegistry()
    names = {d.name for d in reg.all()}
    assert names == {
        "book_appointment", "cancel_appointment", "reschedule_appointment", "send_sms", "execute_api",
    }


def test_default_registry_has_cancel_appointment():
    reg = ToolRegistry()
    defn = reg.resolve("cancel_appointment")

    assert defn is not None
    assert defn.category == "calendar"
    assert defn.parameters_schema["required"] == ["attendee_phone"]
    # No booking_id/uid parameter — a real caller never has that
    # memorized; disambiguation happens via requested_datetime_hint instead.
    assert "booking_id" not in defn.parameters_schema["properties"]


def test_default_registry_has_reschedule_appointment():
    reg = ToolRegistry()
    defn = reg.resolve("reschedule_appointment")

    assert defn is not None
    assert defn.category == "calendar"
    assert set(defn.parameters_schema["required"]) == {"attendee_phone", "new_requested_datetime"}
    assert "booking_id" not in defn.parameters_schema["properties"]


def test_to_generic_schema_shape():
    reg = ToolRegistry()
    defn = reg.resolve("book_appointment")
    schema = defn.to_generic_schema()

    assert schema["name"] == "book_appointment"
    assert schema["parameters"] is defn.parameters_schema
