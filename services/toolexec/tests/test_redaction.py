"""
Unit tests for services/toolexec/redaction.py (T9) — no DB, no network.
"""

from __future__ import annotations

from services.toolexec import redaction


def test_redacts_nested_response_path_leaving_siblings_untouched():
    payload = {
        "customer": {"name": "Ada", "ssn": "123-45-6789", "address": {"city": "Boston"}},
        "order_id": "o-1",
    }
    result = redaction.redact(payload, ["$.customer.ssn"])

    assert result["customer"]["ssn"] == "[redacted]"
    # Siblings at every level are untouched.
    assert result["customer"]["name"] == "Ada"
    assert result["customer"]["address"]["city"] == "Boston"
    assert result["order_id"] == "o-1"
    # Original payload is not mutated.
    assert payload["customer"]["ssn"] == "123-45-6789"


def test_redacts_array_index_at_depth():
    payload = {"items": [{"id": "x", "card": "4111-1111"}, {"id": "y", "card": "5555-2222"}]}
    result = redaction.redact(payload, ["$.items[1].card"])

    assert result["items"][1]["card"] == "[redacted]"
    assert result["items"][0]["card"] == "4111-1111"  # sibling element untouched
    assert result["items"][1]["id"] == "y"  # sibling field untouched


def test_redacts_sensitive_param_name_shorthand_in_flat_arguments():
    arguments = {"order_id": "o-1", "national_id": "999-99-9999"}
    result = redaction.redact(arguments, ["national_id"])

    assert result["national_id"] == "[redacted]"
    assert result["order_id"] == "o-1"


def test_missing_path_is_a_no_op():
    payload = {"data": {"id": "o-1"}}
    result = redaction.redact(payload, ["$.data.ssn", "no_such_key"])
    assert result == payload
