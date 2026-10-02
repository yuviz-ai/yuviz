"""
OllamaLLM — streaming text generation via a local Ollama instance.

Ollama exposes a REST API at http://localhost:11434.
Streaming is done over newline-delimited JSON (not SSE).

pip install httpx
Ollama must be running: https://ollama.com
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

import httpx

from ..interfaces import ChatMessage
from . import build_chat_messages
from ...tools.llm_adapter import TokenEvent, ToolCallEvent, TurnEvent

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:11434"

# Ollama evicts idle models after 5m by default; a reload costs ~15s on the next first turn.
_KEEP_ALIVE = "30m"


def _to_ollama_message(m: dict[str, Any]) -> dict[str, Any]:
    """Convert a generic message to Ollama's wire format (tool calls nested under "function")."""
    if not m.get("tool_calls"):
        return {"role": m["role"], "content": m["content"]}
    return {
        "role": m["role"],
        "content": m["content"],
        "tool_calls": [
            {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in m["tool_calls"]
        ],
    }


class OllamaLLM:
    """ILLM backed by Ollama's /api/chat endpoint.

    think=None omits the field (model default); set False to stop thinking models adding 5-8s/turn.
    """

    def __init__(
        self,
        model:       str = "llama3",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        base_url:    str = _DEFAULT_BASE_URL,
        timeout_s:   float = 30.0,
        think:       bool | None = None,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._base_url    = base_url.rstrip("/")
        self._timeout     = timeout_s
        self._think       = think
        # Set after a 400 rejecting `think`, so later turns stop sending it.
        self._think_unsupported = False
        self._client      = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
        )
        log.info("OllamaLLM model=%s base_url=%s think=%s", model, self._base_url, think)

    async def _stream_chat_lines(self, payload: dict[str, Any]) -> AsyncGenerator[str, None]:
        """POST /api/chat; on a 400 rejecting `think`, retry without it and remember."""
        if self._think_unsupported:
            payload = {k: v for k, v in payload.items() if k != "think"}
        if "think" not in payload:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    yield line
            return

        rejected = False
        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            if resp.status_code == 400:
                body = await resp.aread()
                rejected = b"think" in body.lower()
                if not rejected:
                    resp.raise_for_status()
            else:
                resp.raise_for_status()
            if not rejected:
                async for line in resp.aiter_lines():
                    yield line

        if rejected:
            log.warning(
                "OllamaLLM: model=%s rejected think=%r — retrying without it "
                "and disabling think for this instance",
                self._model, payload["think"],
            )
            self._think_unsupported = True
            fallback = {k: v for k, v in payload.items() if k != "think"}
            async with self._client.stream("POST", "/api/chat", json=fallback) as resp2:
                resp2.raise_for_status()
                async for line in resp2.aiter_lines():
                    yield line

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        all_messages = build_chat_messages(self._system, messages)

        payload = {
            "model":   self._model,
            "messages": all_messages,
            "stream":  True,
            "options": {"temperature": self._temperature},
            "keep_alive": _KEEP_ALIVE,
        }
        if self._think is not None:
            payload["think"] = self._think

        async for line in self._stream_chat_lines(payload):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.warning("OllamaLLM: malformed JSON line=%r", line)
                continue
            if data.get("done"):
                break
            token = data.get("message", {}).get("content", "")
            if token:
                yield token

    async def warm(self) -> None:
        # Load the model into memory before the first live call.
        try:
            async for _ in self.generate([ChatMessage(role="user", content="Hi")]):
                pass
        except Exception:
            log.exception("OllamaLLM: warm() failed model=%s", self._model)

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        """Tool-aware generate(); tool calls arrive whole in one chunk.

        No force-a-tool primitive, so a forced tool_choice narrows the tools list instead."""
        all_messages = [_to_ollama_message(m) for m in build_chat_messages(self._system, messages)]
        effective_schemas = schemas
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            forced_name = tool_choice.get("function", {}).get("name")
            narrowed = [s for s in schemas if s.get("name") == forced_name]
            if narrowed:
                effective_schemas = narrowed
        payload = {
            "model":    self._model,
            "messages": all_messages,
            "stream":   True,
            "tools":    [{"type": "function", "function": s} for s in effective_schemas],
            "options":  {"temperature": self._temperature},
            "keep_alive": _KEEP_ALIVE,
        }
        if self._think is not None:
            payload["think"] = self._think

        async for line in self._stream_chat_lines(payload):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.warning("OllamaLLM: malformed JSON line=%r", line)
                continue
            if data.get("done"):
                break
            message = data.get("message", {})
            tool_calls = message.get("tool_calls") or []
            for i, call in enumerate(tool_calls):
                fn = call.get("function", {})
                yield ToolCallEvent(
                    tool_call_id=call.get("id") or f"call_{i}",
                    tool_name=fn.get("name", ""),
                    arguments=fn.get("arguments") or {},
                )
            if tool_calls:
                return
            token = message.get("content", "")
            if token:
                yield TokenEvent(text=token)

    async def aclose(self) -> None:
        await self._client.aclose()
