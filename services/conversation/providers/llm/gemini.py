"""
GeminiLLM — streaming text generation via Gemini's streamGenerateContent (SSE).

Quirks: top-level system_instruction; assistant role is "model"; a functionCall's
thoughtSignature must be echoed back verbatim on replay or the request 400s.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

import httpx

from ..interfaces import ChatMessage
from . import build_chat_messages, raise_with_body_logged
from ...tools.llm_adapter import TokenEvent, ToolCallEvent, TurnEvent

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


def _tool_name_for_call_id(shaped: list[dict[str, Any]], tool_call_id: str | None) -> str:
    """Find the tool name for a tool_call_id (functionResponse requires the name)."""
    for m in shaped:
        for call in m.get("tool_calls") or []:
            if call.get("id") == tool_call_id:
                return call.get("name", "")
    return ""


class GeminiLLM:
    """ILLM backed by Gemini's streamGenerateContent endpoint."""

    def __init__(
        self,
        api_key:     str,
        model:       str = "gemini-flash-latest",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        base_url:    str = _DEFAULT_BASE_URL,
        # Short: the stream sometimes never sends a first byte; RetryOnceLLM retries.
        timeout_s:   float = 10.0,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._api_key      = api_key
        self._client      = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)
        log.info("GeminiLLM model=%s", model)

    def _shape_contents(self, messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
        """Map tool calls to functionCall parts and tool results to functionResponse on a user turn."""
        shaped = build_chat_messages(self._system, messages)
        system_instruction = None
        contents: list[dict[str, Any]] = []
        # Results of flattened calls must be flattened too: functionResponse needs a native functionCall.
        flattened_call_ids: set[str] = set()
        for m in shaped:
            if m["role"] == "system":
                system_instruction = m["content"]
                continue
            if m.get("tool_calls"):
                # Foreign tool calls lack a thought_signature and would 400; render as text.
                if not all((c.get("provider_metadata") or {}).get("thought_signature") for c in m["tool_calls"]):
                    flattened_call_ids.update(c["id"] for c in m["tool_calls"])
                    summary = "; ".join(f"{c['name']}({json.dumps(c['arguments'])})" for c in m["tool_calls"])
                    contents.append({"role": "model", "parts": [{"text": f"[called {summary}]"}]})
                    continue
                parts = []
                for c in m["tool_calls"]:
                    part: dict[str, Any] = {"functionCall": {"name": c["name"], "args": c["arguments"]}}
                    part["thoughtSignature"] = c["provider_metadata"]["thought_signature"]
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
                continue
            if m["role"] == "tool":
                if m.get("tool_call_id") in flattened_call_ids:
                    contents.append({"role": "user", "parts": [{"text": f"[tool result: {m['content']}]"}]})
                    continue
                try:
                    response_obj = json.loads(m["content"]) if m["content"] else {}
                except json.JSONDecodeError:
                    response_obj = {"result": m["content"]}
                contents.append({"role": "user", "parts": [{"functionResponse": {
                    "name": _tool_name_for_call_id(shaped, m.get("tool_call_id")), "response": response_obj,
                }}]})
                continue
            role = "model" if m["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        return system_instruction, contents

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        system_instruction, contents = self._shape_contents(messages)

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self._temperature},
        }
        if system_instruction:
            payload["system_instruction"] = {"parts": [{"text": system_instruction}]}

        path = f"/v1beta/models/{self._model}:streamGenerateContent"
        # Key in the header, never the query string: httpx logs full URLs at
        # INFO, so ?key=... put a live key in the container logs.
        params = {"alt": "sse"}
        headers = {"x-goog-api-key": self._api_key}

        async with self._client.stream("POST", path, params=params, headers=headers, json=payload) as resp:
            await raise_with_body_logged(resp, log=log, provider="GeminiLLM")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    log.warning("GeminiLLM: malformed JSON line=%r", line)
                    continue
                candidates = data.get("candidates") or []
                if not candidates:
                    continue
                for part in candidates[0].get("content", {}).get("parts", []):
                    token = part.get("text", "")
                    if token:
                        yield token

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        """Tool-aware generate(); a forced tool_choice maps to function_calling_config mode=ANY."""
        system_instruction, contents = self._shape_contents(messages)

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self._temperature},
            "tools": [{"functionDeclarations": schemas}],
        }
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            forced_name = tool_choice.get("function", {}).get("name")
            if forced_name:
                payload["tool_config"] = {
                    "function_calling_config": {"mode": "ANY", "allowed_function_names": [forced_name]},
                }
        if system_instruction:
            payload["system_instruction"] = {"parts": [{"text": system_instruction}]}

        path = f"/v1beta/models/{self._model}:streamGenerateContent"
        # Key in the header, never the query string: httpx logs full URLs at
        # INFO, so ?key=... put a live key in the container logs.
        params = {"alt": "sse"}
        headers = {"x-goog-api-key": self._api_key}

        async with self._client.stream("POST", path, params=params, headers=headers, json=payload) as resp:
            await raise_with_body_logged(resp, log=log, provider="GeminiLLM")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    log.warning("GeminiLLM: malformed JSON line=%r", line)
                    continue
                candidates = data.get("candidates") or []
                if not candidates:
                    continue
                saw_tool_call = False
                for i, part in enumerate(candidates[0].get("content", {}).get("parts", [])):
                    fn_call = part.get("functionCall")
                    if fn_call:
                        saw_tool_call = True
                        sig = part.get("thoughtSignature")
                        yield ToolCallEvent(
                            tool_call_id=fn_call.get("id") or f"call_{i}",
                            tool_name=fn_call.get("name", ""),
                            arguments=fn_call.get("args") or {},
                            provider_metadata={"thought_signature": sig} if sig else None,
                        )
                        continue
                    token = part.get("text", "")
                    if token:
                        yield TokenEvent(text=token)
                if saw_tool_call:
                    return

    async def aclose(self) -> None:
        await self._client.aclose()
