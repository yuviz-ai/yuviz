"""AnthropicLLM tests via httpx.MockTransport, using documented Messages API event shapes."""

from __future__ import annotations

import json

import httpx
import pytest

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.providers.llm.anthropic import AnthropicLLM
from services.conversation.tools.llm_adapter import ToolCallEvent, TokenEvent


def _sse(*events: dict) -> bytes:
    return ("\n".join("data: " + json.dumps(e) for e in events) + "\n").encode()


def _text_body(*texts: str) -> bytes:
    events = [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
    events += [
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}
        for t in texts
    ]
    events += [{"type": "content_block_stop", "index": 0}, {"type": "message_stop"}]
    return _sse(*events)


def _tool_call_body(name: str, args: dict, *, split: bool = True) -> bytes:
    """Split by default to exercise accumulation across chunks."""
    raw = json.dumps(args)
    fragments = [raw[: len(raw) // 2], raw[len(raw) // 2 :]] if split else [raw]
    events = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_01", "name": name, "input": {}}},
    ]
    events += [
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": f}}
        for f in fragments
    ]
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    return _sse(*events)


def _make_llm(handler) -> AnthropicLLM:
    llm = AnthropicLLM(api_key="test-key")
    llm._client = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        headers={"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
        transport=httpx.MockTransport(handler),
    )
    return llm


async def test_generate_yields_streamed_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        # Credential in a header, never the query string — see gemini.py.
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "key" not in request.url.params
        return httpx.Response(200, content=_text_body("Hello", " world"))

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["Hello", " world"]


async def test_generate_sends_system_as_top_level_field_not_a_message():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_text_body("ok"))

    llm = _make_llm(handler)
    _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert "voice assistant" in seen["system"]
    assert [m["role"] for m in seen["messages"]] == ["user"]
    assert seen["max_tokens"] == 4096  # required by the API — omitting is a 400


async def test_generate_uses_caller_supplied_system_prompt():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_text_body("ok"))

    llm = _make_llm(handler)
    messages = [
        ChatMessage(role="system", content="custom prompt"),
        ChatMessage(role="user", content="hi"),
    ]
    _ = [tok async for tok in llm.generate(messages)]

    assert seen["system"] == "custom prompt"
    assert [m["role"] for m in seen["messages"]] == ["user"]


async def test_generate_ignores_malformed_json_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            b"data: not-json\n"
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n'
        )
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["ok"]


async def test_generate_raises_on_mid_stream_error_event():
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n'
            b'data: {"type":"error","error":{"type":"overloaded_error"}}\n'
        )
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    tokens = []
    with pytest.raises(RuntimeError, match="overloaded_error"):
        async for tok in llm.generate([ChatMessage(role="user", content="hi")]):
            tokens.append(tok)

    assert tokens == ["hi"]  # whatever arrived before the error still reached the caller


SCHEMAS = [{
    "name": "book_appointment",
    "description": "Book a slot",
    "parameters": {"type": "object", "properties": {"start_time": {"type": "string"}}},
}]


async def test_generate_with_tools_renames_parameters_to_input_schema():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_text_body("ok"))

    llm = _make_llm(handler)
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], SCHEMAS)]

    assert seen["tools"] == [{
        "name": "book_appointment",
        "description": "Book a slot",
        "input_schema": SCHEMAS[0]["parameters"],
    }]


async def test_generate_with_tools_accumulates_input_json_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_tool_call_body(
            "book_appointment", {"start_time": "2026-07-23T15:00:00"},
        ))

    llm = _make_llm(handler)
    events = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="book 3pm tomorrow")], SCHEMAS,
    )]

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool_call_id == "toolu_01"
    assert events[0].tool_name == "book_appointment"
    assert events[0].arguments == {"start_time": "2026-07-23T15:00:00"}


async def test_generate_with_tools_still_streams_plain_text_incrementally():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_text_body("Hel", "lo"))

    llm = _make_llm(handler)
    events = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="say hi")], SCHEMAS,
    )]

    assert events == [TokenEvent(text="Hel"), TokenEvent(text="lo")]


async def test_generate_with_tools_shapes_tool_call_and_result_natively():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_text_body("done"))

    llm = _make_llm(handler)
    messages = [
        ChatMessage(role="user", content="book 3pm"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "toolu_01", "name": "book_appointment", "arguments": {"start_time": "3pm"}},
        ]),
        ChatMessage(role="tool", content='{"status": "success"}', tool_call_id="toolu_01"),
    ]
    _ = [e async for e in llm.generate_with_tools(messages, SCHEMAS)]

    assistant, tool_result = seen["messages"][1], seen["messages"][2]
    # An empty text block is a 400, so a tool-only turn has just tool_use.
    assert assistant == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01", "name": "book_appointment",
         "input": {"start_time": "3pm"}},
    ]}
    # Anthropic has no "tool" role — a result rides on a user turn.
    assert tool_result == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": '{"status": "success"}'},
    ]}


async def test_generate_with_tools_translates_forced_tool_choice():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_text_body("ok"))

    llm = _make_llm(handler)
    forced = {"type": "function", "function": {"name": "book_appointment"}}
    _ = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="hi")], SCHEMAS, tool_choice=forced,
    )]

    assert seen["tool_choice"] == {"type": "tool", "name": "book_appointment"}


async def test_generate_with_tools_omits_tool_choice_by_default():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_text_body("ok"))

    llm = _make_llm(handler)
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], SCHEMAS)]

    assert "tool_choice" not in seen


async def test_generate_with_tools_survives_malformed_tool_input():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_01", "name": "book_appointment", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": "{not json"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ))

    llm = _make_llm(handler)
    events = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="book it")], SCHEMAS,
    )]

    assert events[0].arguments == {}


# Sampling params 400 on newer models, so check every catalogued id.
async def test_payload_matches_each_catalogued_model_capability():
    cases = {
        "claude-haiku-4-5": True,   # 4.5 still accepts temperature
        "claude-sonnet-5":  False,
        "claude-opus-5":    False,
    }
    for model, takes_temperature in cases.items():
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, content=_text_body("ok"))

        llm = _make_llm(handler)
        llm._model = model
        _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

        assert seen["model"] == model
        assert ("temperature" in seen) is takes_temperature, model
        assert ("output_config" in seen) is not takes_temperature, model
        if not takes_temperature:
            assert seen["output_config"] == {"effort": "low"}
