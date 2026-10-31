from services.conversation.directives import DirectiveParser, EndCallDirective, StreamBuffer


def test_parse_strips_markdown_characters():
    result = DirectiveParser.parse("- **Date:** Tomorrow\n- **Time:** 10:00 AM")
    assert "*" not in result.clean_text
    assert result.clean_text == "- Date: Tomorrow\n- Time: 10:00 AM"


def test_parse_strips_markdown_and_directives_together():
    result = DirectiveParser.parse("**Confirmed.** [[END_CALL]]")
    assert result.clean_text == "Confirmed. "
    assert result.directives == [EndCallDirective()]


def test_streambuffer_split_markdown_marker_across_chunks():
    buf = StreamBuffer()
    safe = buf.feed("The time is *") + buf.feed("*10:00 AM**")
    result = DirectiveParser.parse(safe + buf.flush())
    assert "*" not in result.clean_text
    assert result.clean_text == "The time is 10:00 AM"


import pytest

from services.conversation.directives import strip_markdown_chars


@pytest.mark.parametrize("text", [
    "qa_pwn.aiff", "order_status", "john_doe@example.com", "ABC_123", "a_b_c", "C#", "order #42", "snake_case_name",
])
def test_strip_markdown_keeps_content_chars(text):
    assert strip_markdown_chars(text) == text


@pytest.mark.parametrize("text,expected", [
    ("**bold**", "bold"),
    ("_italic_ word", "italic word"),
    ("__bold__", "bold"),
    ("`code`", "code"),
    ("## Heading", " Heading"),
    ("# Title\nbody", " Title\nbody"),
    ("see _qa_pwn_ file", "see qa_pwn file"),
    ("_", ""),
    ("*_`", ""),
    ("", ""),
])
def test_strip_markdown_removes_markup(text, expected):
    assert strip_markdown_chars(text) == expected


@pytest.mark.parametrize("text", ["qa_pwn **x** _y_ `z` ## h", "___", "a__b", "**_a_b_**"])
def test_strip_markdown_idempotent(text):
    once = strip_markdown_chars(text)
    assert strip_markdown_chars(once) == once


def test_parse_keeps_underscore_identifier_and_strips_tag():
    r = DirectiveParser.parse("Your order_status is **ready** [[END_CALL]]")
    assert r.clean_text.strip() == "Your order_status is ready"
    assert len(r.directives) == 1
