"""OpenAILLM tests via httpx.MockTransport; no real network calls."""

from __future__ import annotations

import httpx

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.providers.llm.openai import OpenAILLM


def _sse_body(*tokens: str) -> bytes:
    lines = []
    for tok in tokens:
        lines.append(
            'data: {"choices":[{"delta":{"content":' + repr(tok).replace("'", '"') + "}}]}"
        )
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode()


def _make_llm(handler) -> OpenAILLM:
    llm = OpenAILLM(api_key="test-key")
    llm._client = httpx.AsyncClient(
        base_url="https://api.openai.com",
        headers={"Authorization": "Bearer test-key"},
        transport=httpx.MockTransport(handler),
    )
    return llm


async def test_generate_yields_streamed_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, content=_sse_body("Hello", " world"))

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["Hello", " world"]


async def test_generate_prepends_system_prompt_when_absent():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(__import__("json").loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    llm = _make_llm(handler)
    _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert seen_payload["messages"][0]["role"] == "system"


async def test_generate_skips_default_system_prompt_when_caller_supplied_one():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(__import__("json").loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    llm = _make_llm(handler)
    messages = [
        ChatMessage(role="system", content="custom prompt"),
        ChatMessage(role="user", content="hi"),
    ]
    _ = [tok async for tok in llm.generate(messages)]

    system_msgs = [m for m in seen_payload["messages"] if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"] == "custom prompt"


async def test_generate_ignores_malformed_json_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        body = b'data: not-json\ndata: {"choices":[{"delta":{"content":"ok"}}]}\ndata: [DONE]\n'
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["ok"]


async def test_generate_ignores_reasoning_only_deltas():
    """Reasoning-only deltas (no "content" key) never surface as tokens on the plain path."""
    body = (
        b'data: {"choices":[{"delta":{"reasoning":"Thinking...","channel":"analysis"}}]}\n'
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n'
        b"data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["Hello"]


# ── generate_with_tools() (also used via Groq, OpenAI-compatible) ─────────────

def _tool_call_chunk_sse(*chunks: str) -> bytes:
    lines = [c if c.startswith("data: ") else f"data: {c}" for c in chunks]
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode()


async def test_generate_with_tools_single_complete_chunk_shape():
    """Groq sends the whole tool_call in one chunk, not built up incrementally."""
    import json as _json
    from services.conversation.tools.llm_adapter import ToolCallEvent

    body = _tool_call_chunk_sse(
        _json.dumps({"choices": [{"delta": {"role": "assistant", "content": None}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_abc", "type": "function",
            "function": {"name": "book_appointment", "arguments": '{"requested_datetime":"2026-07-25T14:00:00"}'},
        }]}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    events = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="book me")], [{"name": "book_appointment", "parameters": {}}],
    )]

    assert events == [ToolCallEvent(
        tool_call_id="call_abc", tool_name="book_appointment",
        arguments={"requested_datetime": "2026-07-25T14:00:00"},
    )]


async def test_generate_with_tools_incrementally_streamed_arguments():
    """Real OpenAI's documented behavior: function.arguments streamed as
    fragments across many chunks, same index, concatenated here."""
    import json as _json
    from services.conversation.tools.llm_adapter import ToolCallEvent

    body = _tool_call_chunk_sse(
        _json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_xyz", "type": "function", "function": {"name": "book_appointment", "arguments": ""}},
        ]}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"requested_datetime":'}},
        ]}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"2026-07-25T14:00:00"}'}},
        ]}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    events = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="book me")], [{"name": "book_appointment", "parameters": {}}],
    )]

    assert events == [ToolCallEvent(
        tool_call_id="call_xyz", tool_name="book_appointment",
        arguments={"requested_datetime": "2026-07-25T14:00:00"},
    )]


async def test_generate_with_tools_forwards_tool_choice_when_given():
    """A caller-supplied tool_choice reaches the request payload verbatim."""
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(__import__("json").loads(request.content))
        return httpx.Response(200, content=_tool_call_chunk_sse('{"choices": [{"delta": {}, "finish_reason": "stop"}]}'))

    llm = _make_llm(handler)
    forced = {"type": "function", "function": {"name": "book_appointment"}}
    _ = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="hi")], [{"name": "book_appointment", "parameters": {}}],
        tool_choice=forced,
    )]

    assert seen_payload["tool_choice"] == forced


async def test_generate_with_tools_omits_tool_choice_by_default():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(__import__("json").loads(request.content))
        return httpx.Response(200, content=_tool_call_chunk_sse('{"choices": [{"delta": {}, "finish_reason": "stop"}]}'))

    llm = _make_llm(handler)
    _ = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="hi")], [{"name": "book_appointment", "parameters": {}}],
    )]

    assert "tool_choice" not in seen_payload


async def test_generate_with_tools_ignores_reasoning_only_deltas():
    """Reasoning-only deltas (Groq gpt-oss shape) are never spoken; the real tool call still comes through."""
    import json as _json
    from services.conversation.tools.llm_adapter import ToolCallEvent

    body = _tool_call_chunk_sse(
        _json.dumps({"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"reasoning": "The", "channel": "analysis"}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"reasoning": " user wants", "channel": "analysis"}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"reasoning": " to book.", "channel": "analysis"}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"tool_calls": [{
            "id": "fc_1afc81e1", "type": "function", "index": 0,
            "function": {"name": "book_appointment", "arguments": '{"attendee_name":"Jane","requested_datetime":"2026-08-28T15:00:00"}'},
        }]}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    events = [e async for e in llm.generate_with_tools(
        [ChatMessage(role="user", content="book a demo")], [{"name": "book_appointment", "parameters": {}}],
    )]

    assert events == [ToolCallEvent(
        tool_call_id="fc_1afc81e1", tool_name="book_appointment",
        arguments={"attendee_name": "Jane", "requested_datetime": "2026-08-28T15:00:00"},
    )]


async def test_generate_with_tools_plain_text_yields_token_events():
    import json as _json
    from services.conversation.tools.llm_adapter import TokenEvent

    body = _tool_call_chunk_sse(
        _json.dumps({"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {"content": " there"}, "finish_reason": None}]}),
        _json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    events = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], [])]

    assert events == [TokenEvent(text="Hi"), TokenEvent(text=" there")]
