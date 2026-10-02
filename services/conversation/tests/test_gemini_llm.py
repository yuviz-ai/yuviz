"""GeminiLLM tests via httpx.MockTransport; chunk shapes match the real Gemini wire format."""

from __future__ import annotations

import json

import httpx
import pytest

from services.conversation.providers.interfaces import ChatMessage
from services.conversation.providers.llm.gemini import GeminiLLM
from services.conversation.tools.llm_adapter import ToolCallEvent, TokenEvent


def _sse_body(*texts: str) -> bytes:
    lines = []
    for t in texts:
        chunk = {"candidates": [{"content": {"parts": [{"text": t}], "role": "model"}}]}
        lines.append("data: " + json.dumps(chunk))
    return ("\n".join(lines) + "\n").encode()


def _sse_tool_call_body(name: str, args: dict) -> bytes:
    chunk = {"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": args}}], "role": "model"}}]}
    return ("data: " + json.dumps(chunk) + "\n").encode()


def _make_llm(handler) -> GeminiLLM:
    llm = GeminiLLM(api_key="test-key")
    llm._client = httpx.AsyncClient(
        base_url="https://generativelanguage.googleapis.com",
        transport=httpx.MockTransport(handler),
    )
    return llm


async def test_generate_yields_streamed_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        # Credential in a header, never the query string (see gemini.py).
        assert request.headers["x-goog-api-key"] == "test-key"
        assert "key" not in request.url.params
        return httpx.Response(200, content=_sse_body("Hello", " world"))

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["Hello", " world"]


async def test_generate_sends_default_system_prompt_as_system_instruction_when_absent():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    llm = _make_llm(handler)
    _ = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert "system_instruction" in seen_payload
    assert seen_payload["system_instruction"]["parts"][0]["text"] == llm._system
    # The default system prompt must never leak into contents as a message —
    # Gemini has no "system" role in contents at all.
    assert all(m["role"] != "system" for m in seen_payload["contents"])


async def test_generate_uses_callers_system_message_as_system_instruction():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    llm = _make_llm(handler)
    messages = [
        ChatMessage(role="system", content="custom prompt"),
        ChatMessage(role="user", content="hi"),
    ]
    _ = [tok async for tok in llm.generate(messages)]

    assert seen_payload["system_instruction"]["parts"][0]["text"] == "custom prompt"
    assert all(m["role"] != "system" for m in seen_payload["contents"])


async def test_generate_maps_assistant_role_to_model():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    llm = _make_llm(handler)
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello there"),
        ChatMessage(role="user", content="how are you"),
    ]
    _ = [tok async for tok in llm.generate(messages)]

    roles = [m["role"] for m in seen_payload["contents"]]
    assert roles == ["user", "model", "user"]


async def test_generate_ignores_malformed_json_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        body = b'data: not-json\ndata: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}\n'
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["ok"]


async def test_generate_ignores_chunks_with_no_candidates():
    # Real Gemini streams sometimes send a trailing chunk carrying only
    # usageMetadata, no candidates — confirmed against the real API.
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            b'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}\n'
            b'data: {"usageMetadata":{"totalTokenCount":42}}\n'
        )
        return httpx.Response(200, content=body)

    llm = _make_llm(handler)
    tokens = [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]

    assert tokens == ["ok"]


async def test_generate_with_tools_sends_function_declarations():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    _ = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="hi")], schemas)]

    assert seen_payload["tools"] == [{"functionDeclarations": schemas}]


async def test_generate_with_tools_yields_tool_call_event():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_tool_call_body("book_appointment", {"start_time": "2026-07-23T15:00:00"}))

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    events = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="book 3pm tomorrow")], schemas)]

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool_name == "book_appointment"
    assert events[0].arguments == {"start_time": "2026-07-23T15:00:00"}


async def test_generate_with_tools_still_streams_plain_text_incrementally():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body("Hi", " there"))

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    events = [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="say hi")], schemas)]

    assert events == [TokenEvent(text="Hi"), TokenEvent(text=" there")]


async def test_generate_with_tools_shapes_tool_call_and_result_natively():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("Booked!"))

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    messages = [
        ChatMessage(role="user", content="book 3pm tomorrow"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {
                "id": "call_abc", "name": "book_appointment",
                "arguments": {"start_time": "2026-07-23T15:00:00"},
                "provider_metadata": {"thought_signature": "sig123"},
            },
        ]),
        ChatMessage(role="tool", content='{"status": "success", "booked": true}', tool_call_id="call_abc"),
    ]
    _ = [e async for e in llm.generate_with_tools(messages, schemas)]

    contents = seen_payload["contents"]
    fn_call_msg = next(c for c in contents if c["parts"][0].get("functionCall"))
    assert fn_call_msg["role"] == "model"
    assert fn_call_msg["parts"][0]["functionCall"] == {"name": "book_appointment", "args": {"start_time": "2026-07-23T15:00:00"}}
    assert fn_call_msg["parts"][0]["thoughtSignature"] == "sig123"

    fn_response_msg = next(c for c in contents if c["parts"][0].get("functionResponse"))
    assert fn_response_msg["role"] == "user"
    assert fn_response_msg["parts"][0]["functionResponse"] == {
        "name": "book_appointment", "response": {"status": "success", "booked": True},
    }


async def test_generate_with_tools_flattens_foreign_tool_call_with_no_thought_signature():
    # A replayed tool call Gemini never made (e.g. Groq's) has no thought_signature, and Gemini
    # 400s on a native functionCall without one; it must render as plain text instead.
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("Booked!"))

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    messages = [
        ChatMessage(role="user", content="book 3pm tomorrow"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "call_abc", "name": "book_appointment", "arguments": {"start_time": "2026-07-23T15:00:00"}},
        ]),
        ChatMessage(role="tool", content='{"status": "success", "booked": true}', tool_call_id="call_abc"),
    ]
    _ = [e async for e in llm.generate_with_tools(messages, schemas)]

    contents = seen_payload["contents"]
    assert not any("functionCall" in c["parts"][0] for c in contents)
    assert not any("functionResponse" in c["parts"][0] for c in contents)
    flattened_call = next(c for c in contents if "book_appointment" in c["parts"][0].get("text", ""))
    assert flattened_call["role"] == "model"
    flattened_result = next(c for c in contents if "success" in c["parts"][0].get("text", ""))
    assert flattened_result["role"] == "user"


# --- Timeout: one attempt per call, raising immediately; retrying is RetryOnceLLM's job.

async def test_generate_raises_immediately_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    llm = _make_llm(handler)
    with pytest.raises(httpx.ReadTimeout):
        [tok async for tok in llm.generate([ChatMessage(role="user", content="hi")])]


async def test_generate_with_tools_raises_immediately_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    llm = _make_llm(handler)
    schemas = [{"name": "book_appointment", "description": "Book it", "parameters": {"type": "object"}}]
    with pytest.raises(httpx.ReadTimeout):
        [e async for e in llm.generate_with_tools([ChatMessage(role="user", content="book it")], schemas)]
