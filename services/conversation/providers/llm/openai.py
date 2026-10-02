"""
OpenAILLM — streaming text generation via OpenAI's /v1/chat/completions (SSE).

Also backs Groq (OpenAI-compatible; only base_url differs).
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

_DEFAULT_BASE_URL = "https://api.openai.com"


def _to_openai_message(m: dict[str, Any]) -> dict[str, Any]:
    """Convert a generic message to OpenAI's wire format ("arguments" must be a JSON string)."""
    if not m.get("tool_calls"):
        return {"role": m["role"], "content": m["content"], **({"tool_call_id": m["tool_call_id"]} if m.get("tool_call_id") else {})}
    return {
        "role": m["role"],
        "content": m["content"],
        "tool_calls": [
            {
                "id": c["id"], "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
            }
            for c in m["tool_calls"]
        ],
    }


class OpenAILLM:
    """ILLM backed by OpenAI's chat completions endpoint."""

    def __init__(
        self,
        api_key:     str,
        model:       str = "gpt-4o",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        base_url:    str = _DEFAULT_BASE_URL,
        # Short: streams can stall after 200 OK, and the gateway gives up at ~20s.
        timeout_s:   float = 10.0,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._client      = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
        log.info("OpenAILLM model=%s", model)

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        # History can still carry tool messages (orchestrator's forced final generation).
        all_messages = [_to_openai_message(m) for m in build_chat_messages(self._system, messages)]

        payload = {
            "model":       self._model,
            "messages":    all_messages,
            "stream":      True,
            "temperature": self._temperature,
        }

        async with self._client.stream(
            "POST", "/v1/chat/completions", json=payload,
        ) as resp:
            await raise_with_body_logged(resp, log=log, provider="OpenAILLM")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    log.warning("OpenAILLM: malformed JSON line=%r", line)
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                token = choices[0].get("delta", {}).get("content", "")
                if token:
                    yield token

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        all_messages = [_to_openai_message(m) for m in build_chat_messages(self._system, messages)]
        tools = [{"type": "function", "function": s} for s in schemas]

        payload = {
            "model":       self._model,
            "messages":    all_messages,
            "stream":      True,
            "temperature": self._temperature,
            "tools":       tools,
        }
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        # Keyed by index: Groq sends a call whole, OpenAI streams arguments across chunks.
        accumulating: dict[int, dict[str, Any]] = {}

        async with self._client.stream(
            "POST", "/v1/chat/completions", json=payload,
        ) as resp:
            await raise_with_body_logged(resp, log=log, provider="OpenAILLM")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    log.warning("OpenAILLM: malformed JSON line=%r", line)
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = accumulating.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["name"] = fn["name"]
                    if fn.get("arguments"):
                        entry["arguments"] += fn["arguments"]

                token = delta.get("content") or ""
                if token:
                    yield TokenEvent(text=token)

                if choice.get("finish_reason") == "tool_calls":
                    for i, entry in accumulating.items():
                        try:
                            args = json.loads(entry["arguments"]) if entry["arguments"] else {}
                        except json.JSONDecodeError:
                            log.warning("OpenAILLM: malformed tool_call arguments=%r", entry["arguments"])
                            args = {}
                        yield ToolCallEvent(
                            tool_call_id=entry["id"] or f"call_{i}",
                            tool_name=entry["name"] or "",
                            arguments=args,
                        )
                    return

    async def aclose(self) -> None:
        await self._client.aclose()
