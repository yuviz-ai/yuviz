"""
GeminiLLM — streaming text generation via Google's Gemini API.

Streaming is Server-Sent Events (SSE) over the streamGenerateContent
endpoint: lines prefixed "data: ", each a JSON chunk carrying an
incremental candidates[0].content.parts[].text — same token-yielding
contract as OllamaLLM/OpenAILLM, different wire format.

Gemini's message shape differs from OpenAI/Ollama's in three ways this
class has to bridge, not push onto callers: (1) the system prompt is a
top-level system_instruction field, never a "system"-role message inside
contents; (2) the assistant's own role is named "model", not "assistant";
(3) a functionCall part returned with tool-calling carries a sibling
"thoughtSignature" field (Gemini's "thinking" models) that MUST be
echoed back verbatim on that same functionCall
part when it's replayed into history for a later turn — omitting it is a
hard 400 (INVALID_ARGUMENT: "missing a thought_signature"), not a
degraded-quality warning. Carried end-to-end via ToolCallEvent/ChatMessage's
generic provider_metadata passthrough (see llm_adapter.py) so the
orchestrator never has to know this exists.
build_chat_messages() still owns the "does the caller already have a
system message" precedence decision — this class only re-shapes its
output for Gemini's wire format afterward.

Auth is the x-goog-api-key header — never the "key" query parameter, which
httpx logs at INFO (see generate()).

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


def _gemini_nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Every other provider (OpenAI/Ollama/Anthropic) accepts JSON Schema's
    own union-type form ({"type": ["string", "null"]}) for an optional
    field — registry.py's tool definitions use exactly that shape. Gemini's
    functionDeclarations schema doesn't: it wants a single `type` string
    plus a separate `nullable: true`, and rejects a type array outright.
    Recurses into `properties` (object) and `items` (array) since a nullable
    field can appear nested, not just at the top level. Gemini-only — the
    generic schema handed to every other provider is untouched."""
    out = dict(schema)
    type_value = out.get("type")
    if isinstance(type_value, list):
        non_null = [t for t in type_value if t != "null"]
        if non_null:
            out["type"] = non_null[0] if len(non_null) == 1 else non_null
        else:
            out.pop("type", None)
        if "null" in type_value:
            out["nullable"] = True
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _gemini_nullable_schema(v) for k, v in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _gemini_nullable_schema(out["items"])
    return out

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


def _tool_name_for_call_id(shaped: list[dict[str, Any]], tool_call_id: str | None) -> str:
    """Gemini's functionResponse needs the original call's tool name, which
    a "tool"-role ChatMessage only references indirectly via
    tool_call_id — looked up from whichever earlier message's tool_calls
    entry has a matching id (a tool-result message always follows its own
    assistant tool_calls message in a well-formed history)."""
    for m in shaped:
        for call in m.get("tool_calls") or []:
            if call.get("id") == tool_call_id:
                return call.get("name", "")
    return ""


class GeminiLLM:
    """
    ILLM implementation backed by Google's Gemini streamGenerateContent endpoint.

    api_key     — resolved once at construction by AIProviderManager via
                  SecretResolver, never re-resolved per call.
    model       — e.g. "gemini-flash-latest" (an alias Google keeps pointed
                  at its current stable fast model — recommended default,
                  since pinned version strings get deprecated for new
                  callers over time), "gemini-2.5-pro"
    system      — system prompt sent as Gemini's system_instruction when
                  the caller hasn't already injected one (same precedent
                  as OllamaLLM.generate()).
    temperature — sampling temperature (0.0 = deterministic)
    """

    def __init__(
        self,
        api_key:     str,
        model:       str = "gemini-flash-latest",
        system:      str = "You are a helpful voice assistant. "
                          "Keep responses concise and natural for speech.",
        temperature: float = 0.7,
        base_url:    str = _DEFAULT_BASE_URL,
        # 30s left a caller sitting in dead air for a full 30 seconds — Gemini's
        # streamGenerateContent endpoint occasionally never sends even its
        # first byte (httpx.ReadTimeout while still waiting on response
        # headers, not a slow-but-progressing stream). 10s cuts that wait
        # dramatically; RetryOnceLLM (provider_bundle.py) is what makes this
        # safe to shorten — a genuinely slow-but-working request that trips
        # this timeout still gets one retry before failing the turn.
        timeout_s:   float = 10.0,
    ) -> None:
        self._model       = model
        self._system      = system
        self._temperature = temperature
        self._api_key      = api_key
        self._client      = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)
        log.info("GeminiLLM model=%s", model)

    def _shape_contents(self, messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
        """Bridges two Gemini-specific shapes build_chat_messages()'s generic
        output doesn't know about: a tool_calls-bearing assistant message
        becomes a functionCall part (args, not "arguments"), and a
        "tool"-role message becomes a functionResponse
        part on a "user"-role turn — Gemini has no distinct tool role at
        all. functionResponse.response must be a JSON object, not a string,
        so a tool message's content (always a JSON string by convention —
        see ChatMessage's docstring) is parsed back into one here."""
        shaped = build_chat_messages(self._system, messages)
        system_instruction = None
        contents: list[dict[str, Any]] = []
        # Tool-call ids whose originating assistant message got flattened to
        # plain text below (foreign-origin, no thought_signature) — the
        # paired "tool"-role result must follow the same path, since a
        # native functionResponse only makes sense pointing at a native
        # functionCall right before it.
        flattened_call_ids: set[str] = set()
        for m in shaped:
            if m["role"] == "system":
                system_instruction = m["content"]
                continue
            if m.get("tool_calls"):
                # A tool call this class itself never produced — e.g. Groq's
                # or OpenAI's, replayed into history if this engine was ever
                # switched to mid-conversation — carries no thought_signature,
                # and Gemini's native functionCall part hard-400s without one
                # ("missing a thought_signature") once any tool-calling has
                # happened. There's no signature to echo back that this
                # class didn't invent, so render it as plain text instead of
                # Gemini's native function-calling grammar, which this
                # message was never part of. Confirmed live: this broke a
                # real call mid-booking-flow the first time this happened.
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

        # No retry here — RetryOnceLLM (provider_bundle.py) already wraps
        # every ILLM, including this one, and retries once on any exception
        # raised before a token yields. A second, provider-local retry loop
        # used to live here too, which meant Gemini alone got double-
        # retried against every other provider's single retry.
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
        """IToolAwareLLM companion to generate() — same client/auth, additive
        method. Gemini wraps the generic {name, description, parameters}
        schema list into a single functionDeclarations entry, unlike
        OpenAI/Ollama's per-tool wrapper. A functionCall part, like a text
        part, arrives as a complete unit in whichever chunk it appears —
        never built up incrementally the way text streams — so plain-text
        turns keep the same per-sentence TTS latency they have today.

        tool_choice, when given as the same OpenAI-shaped dict every other
        provider accepts ({"type": "function", "function": {"name": ...}}),
        is translated to Gemini's own tool_config.function_calling_config
        (mode="ANY" + allowed_function_names) — Gemini's real equivalent of
        forcing one specific function call instead of leaving it optional."""
        system_instruction, contents = self._shape_contents(messages)

        gemini_schemas = [
            {**s, "parameters": _gemini_nullable_schema(s["parameters"])} if "parameters" in s else s
            for s in schemas
        ]
        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self._temperature},
            "tools": [{"functionDeclarations": gemini_schemas}],
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

        # No retry here — see generate()'s comment: RetryOnceLLM already
        # wraps every provider uniformly, so a second, Gemini-local retry
        # loop would double-retry instead of matching every other engine's
        # single retry.
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
