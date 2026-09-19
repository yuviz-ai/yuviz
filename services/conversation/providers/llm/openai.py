"""
OpenAILLM — streaming text generation via OpenAI's /v1/chat/completions.

Streaming is Server-Sent Events (SSE): lines prefixed "data: ", terminated by
a literal "data: [DONE]" line — different wire format from OllamaLLM's
newline-delimited JSON, same token-yielding contract.

Also backs Groq (2026-07-24): Groq's API is OpenAI-compatible by design —
same request/response shape, same SSE framing — confirmed live against the
real API before this was assumed. The only difference is base_url and
which models exist; see ai_provider_manager.py's _make_groq_llm, which
constructs this same class pointed at Groq's endpoint rather than a
separate GroqLLM file.

generate_with_tools() accumulates delta.tool_calls by their "index" field
across chunks rather than assuming either "one complete chunk" or "streamed
incrementally" — confirmed live that Groq sends a tool call as a single
complete chunk, but real OpenAI is documented to stream tool_call.function.
arguments incrementally across many chunks; accumulating by index handles
both without caring which one a given vendor/request does.

pip install httpx (already a dependency via OllamaLLM)
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
    """build_chat_messages() yields a generic {role, content, tool_calls?,
    tool_call_id?} shape — bridge it to OpenAI's actual wire format here.
    Confirmed live 2026-07-24 (Groq, OpenAI-compatible): passing the
    generic flat tool_calls dicts straight through 400s with 'tool_calls.0.
    type is missing' — OpenAI requires each entry nested under
    {"id", "type": "function", "function": {"name", "arguments"}}, and
    critically "arguments" must be a JSON *string*, not the parsed dict our
    ToolCallEvent/ChatMessage carry internally. A "tool"-role message passes
    through content as-is (a real "tool" role + tool_call_id, same as
    Ollama's bridge — OpenAI has no Gemini-style structured functionResponse
    requirement)."""
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
    """
    ILLM implementation backed by OpenAI's chat completions endpoint.

    api_key     — resolved once at construction by AIProviderManager via
                  SecretResolver, never re-resolved per call.
    model       — e.g. "gpt-4o", "gpt-4o-mini"
    system      — system prompt prepended when the caller hasn't already
                  injected one (same precedent as OllamaLLM.generate()).
    temperature — sampling temperature (0.0 = deterministic)
    """

    def __init__(
        self,
        api_key:     str,
        model:       str = "gpt-4o",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        base_url:    str = _DEFAULT_BASE_URL,
        # 30s (the old default) left a caller in dead air that long when
        # Groq's stream opened (200 OK logged) but then stalled with no
        # token for a long stretch — a real, live-observed failure distinct
        # from the fast-failing 429 rate-limit case, and far longer than
        # the gateway's own ~19-20s patience. Matches GeminiLLM's own
        # timeout_s=10.0, fixed there for the identical reason.
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
        # _to_openai_message: history may still carry prior tool_calls/
        # tool-role messages here even though this plain path offers no
        # tools this turn — e.g. ToolCallOrchestrator's max_tool_iterations
        # forced-final-generation case (see orchestrator.py) — so the same
        # bridge is needed here, not just in generate_with_tools().
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
        # Every call leaves tool_choice unset (API default: "auto"), giving
        # the model full discretion on every turn — confirmed live,
        # repeatedly, that this is a real contributing factor to fabricated
        # booking claims: the model sometimes just declines to call the
        # tool even on the exact turn it obviously should. tool_choice is
        # the caller's lever to force it on that one specific turn (e.g.
        # {"type": "function", "function": {"name": "execute_api"}}), passed
        # through ToolCallOrchestrator.run_turn(force_tool_name=...). No
        # caller sets it today — the phone-confirmation trigger that used
        # to went away with the calendar built-ins — so every turn is
        # currently "auto". This class only forwards whatever it is given.
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        # Keyed by delta.tool_calls[].index — handles a vendor sending the
        # whole call in one chunk (index always 0, one iteration) or
        # streaming .function.arguments incrementally across many chunks
        # (same index, concatenated) uniformly (see module docstring).
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
