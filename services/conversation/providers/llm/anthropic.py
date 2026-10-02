"""
AnthropicLLM — streaming text generation via Anthropic's Messages API (SSE).

Quirks: top-level system field; max_tokens required; no "tool" role (results are
tool_result blocks on a user turn); tool args stream as fragments keyed by block index.
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

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"

# These models 400 on `temperature` and think adaptively by default;
# effort=low curbs thinking and, unlike thinking=disabled, is accepted by all of them.
_EFFORT_MODELS = (
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4-8", "claude-opus-4-7",
)


class AnthropicLLM:
    """ILLM backed by Anthropic's Messages endpoint.

    max_tokens is a runaway ceiling; thinking tokens share it, hence the headroom.
    """

    def __init__(
        self,
        api_key:     str,
        model:       str = "claude-haiku-4-5",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        max_tokens:  int = 4096,
        base_url:    str = _DEFAULT_BASE_URL,
        timeout_s:   float = 30.0,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._max_tokens  = max_tokens
        # Key in a header, never the query string — see gemini.py.
        self._client      = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-api-key": api_key, "anthropic-version": _API_VERSION},
            timeout=timeout_s,
        )
        log.info("AnthropicLLM model=%s", model)

    def _shape_messages(self, messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
        """Re-shape build_chat_messages() output for Anthropic's wire format."""
        shaped = build_chat_messages(self._system, messages)
        system: str | None = None
        out: list[dict[str, Any]] = []
        for m in shaped:
            if m["role"] == "system":
                system = m["content"]
                continue
            if m.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                # An empty text block is a 400, and a tool-only turn has none.
                if m["content"]:
                    blocks.append({"type": "text", "text": m["content"]})
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                    for c in m["tool_calls"]
                ]
                out.append({"role": "assistant", "content": blocks})
                continue
            if m["role"] == "tool":
                out.append({"role": "user", "content": [{
                    "type":        "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content":     m["content"],
                }]})
                continue
            out.append({"role": m["role"], "content": m["content"]})
        return system, out

    def _payload(self, messages: list[ChatMessage]) -> dict[str, Any]:
        system, shaped = self._shape_messages(messages)
        payload: dict[str, Any] = {
            "model":      self._model,
            "messages":   shaped,
            "max_tokens": self._max_tokens,
            "stream":     True,
        }
        if self._model.startswith(_EFFORT_MODELS):
            payload["output_config"] = {"effort": "low"}
        else:
            payload["temperature"] = self._temperature
        if system:
            payload["system"] = system
        return payload

    def _decode(self, line: str) -> dict[str, Any] | None:
        """One "data: " line -> its JSON, or None for anything else."""
        if not line.startswith("data: "):
            return None
        try:
            data = json.loads(line[len("data: "):])
        except json.JSONDecodeError:
            log.warning("AnthropicLLM: malformed JSON line=%r", line)
            return None
        # Errors arrive as 200 SSE lines; raise so the pipeline speaks its fallback.
        if data.get("type") == "error":
            raise RuntimeError(f"AnthropicLLM: stream error event={data.get('error')}")
        return data

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        # _shape_messages handles tool history even here: the orchestrator's
        # forced-final-generation replays it with no tools offered (openai.py).
        async with self._client.stream("POST", "/v1/messages", json=self._payload(messages)) as resp:
            await raise_with_body_logged(resp, log=log, provider="AnthropicLLM")
            async for line in resp.aiter_lines():
                data = self._decode(line)
                if data is None or data.get("type") != "content_block_delta":
                    continue
                token = (data.get("delta") or {}).get("text") or ""
                if token:
                    yield token

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        """Tool-aware generate(); schema "parameters" maps to Anthropic's "input_schema"."""
        payload = self._payload(messages)
        payload["tools"] = [
            {
                "name":         s["name"],
                "description":  s.get("description", ""),
                "input_schema": s.get("parameters") or {},
            }
            for s in schemas
        ]
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            forced_name = tool_choice.get("function", {}).get("name")
            if forced_name:
                payload["tool_choice"] = {"type": "tool", "name": forced_name}

        # Keyed by block index: argument fragments of parallel tool_use blocks interleave.
        accumulating: dict[int, dict[str, Any]] = {}

        async with self._client.stream("POST", "/v1/messages", json=payload) as resp:
            await raise_with_body_logged(resp, log=log, provider="AnthropicLLM")
            async for line in resp.aiter_lines():
                data = self._decode(line)
                if data is None:
                    continue
                event_type = data.get("type")
                index = data.get("index", 0)

                if event_type == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        accumulating[index] = {
                            "id": block.get("id"), "name": block.get("name"), "arguments": [],
                        }

                elif event_type == "content_block_delta":
                    delta = data.get("delta") or {}
                    if index in accumulating:
                        accumulating[index]["arguments"].append(delta.get("partial_json") or "")
                        continue
                    token = delta.get("text") or ""
                    if token:
                        yield TokenEvent(text=token)

                elif event_type == "content_block_stop":
                    entry = accumulating.pop(index, None)
                    if entry is None:
                        continue
                    raw = "".join(entry["arguments"])
                    try:
                        args = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        log.warning("AnthropicLLM: malformed tool_use input=%r", raw)
                        args = {}
                    yield ToolCallEvent(
                        tool_call_id=entry["id"] or f"call_{index}",
                        tool_name=entry["name"] or "",
                        arguments=args,
                    )

    async def aclose(self) -> None:
        await self._client.aclose()
