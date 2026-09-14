"""Background LLM passes: variable extraction on node leave, context summarization.

Out-of-band on the call's LLM — never in conversation context, never fail the call.
Queued during a transition; pipeline starts them after the live generate finishes.
Interrupted when the caller speaks so they cannot contend with the next turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable

from libs.config_sdk.workflow import Node

from ..providers.interfaces import ChatMessage

log = logging.getLogger(__name__)

_EXTRACTION_TIMEOUT_S = 8.0
_SUMMARY_TIMEOUT_S = 8.0

# Default when pipeline does not pass summary_threshold_for(max_history).
# Must stay under trim cap (max_history*2+1); pipeline derives the live value.
_SUMMARY_THRESHOLD_MSGS = 16
# Recent turns kept verbatim — paraphrasing them breaks the live exchange.
_SUMMARY_KEEP_LAST = 4

# Caller-controlled strings reach transfer destinations and prompts — reject
# control chars (ESL newline injection) and cap length (prefill bloat).
_MAX_EXTRACTED_STR_LEN = 256
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_BOOL_TRUE = frozenset({"true", "yes", "1"})
_BOOL_FALSE = frozenset({"false", "no", "0"})

_EXTRACT_SYSTEM = "Reply with a single JSON object only. No prose, no markdown."
_SUMMARY_SYSTEM = "Reply with a short plain-text summary only. No preamble."


def summary_threshold_for(max_history: int) -> int:
    """Threshold under the pipeline trim cap so a summary can still apply."""
    return min(max(_SUMMARY_KEEP_LAST + 2, max_history * 2 - 4), max_history * 2)


def _goto_tool_call_ids(msg: ChatMessage) -> set[str] | None:
    """Ids if every tool_call on this assistant message is a goto_* transition."""
    if msg.role != "assistant" or not msg.tool_calls:
        return None
    names = [str(tc.get("name") or "") for tc in msg.tool_calls]
    if not names or not all(n.startswith("goto_") for n in names):
        return None
    return {str(tc["id"]) for tc in msg.tool_calls if tc.get("id")}


def _strip_transition_noise(history: list[ChatMessage]) -> list[ChatMessage]:
    """Drop goto_* transition calls/results only — keep real tool traffic."""
    drop_ids: set[str] = set()
    for msg in history:
        ids = _goto_tool_call_ids(msg)
        if ids is not None:
            drop_ids |= ids
    kept: list[ChatMessage] = []
    for msg in history:
        if _goto_tool_call_ids(msg) is not None:
            continue
        if msg.role == "tool" and msg.tool_call_id and msg.tool_call_id in drop_ids:
            continue
        kept.append(msg)
    return kept


def _transcript(history: list[ChatMessage]) -> str:
    lines = [
        f"{msg.role}: {msg.content.strip()}"
        for msg in _strip_transition_noise(history)
        if msg.role in ("user", "assistant") and (msg.content or "").strip()
    ]
    return "\n".join(lines)


async def _collect(llm: Any, messages: list[ChatMessage], timeout_s: float) -> str:
    async def _run() -> str:
        chunks: list[str] = []
        async for token in llm.generate(messages):
            chunks.append(token)
        return "".join(chunks)

    return await asyncio.wait_for(_run(), timeout=timeout_s)


def _parse_json_object(text: str) -> dict[str, Any]:
    """Pull a JSON object out of prose or a ```json fence."""
    fenced = _JSON_FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in extraction response: {text[:200]!r}")
    parsed = json.loads(candidate[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("extraction response was not a JSON object")
    return parsed


def _coerce(value: Any, declared_type: str) -> Any:
    if value is None or value == "":
        return None
    if declared_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if declared_type == "boolean":
        if isinstance(value, bool):
            return value
        token = str(value).strip().lower()
        if token in _BOOL_TRUE:
            return True
        if token in _BOOL_FALSE:
            return False
        return None
    text = str(value)
    if _CONTROL_CHARS_RE.search(text):
        return None
    if len(text) > _MAX_EXTRACTED_STR_LEN:
        text = text[:_MAX_EXTRACTED_STR_LEN]
    return text


class VariableExtractor:
    """Queue on extract(); start_deferred() after the live turn so the call LLM is free."""

    def __init__(
        self,
        llm: Any,
        on_variables: Callable[[dict[str, Any]], None],
        timeout_s: float = _EXTRACTION_TIMEOUT_S,
    ) -> None:
        self._llm = llm
        self._on_variables = on_variables
        self._timeout_s = timeout_s
        self._deferred: list[tuple[Node, list[ChatMessage]]] = []
        self._pending: set[asyncio.Task] = set()
        self._inflight: dict[asyncio.Task, tuple[Node, list[ChatMessage]]] = {}
        # Teardown can fire more than once; only one final extract.
        self._final_done = False

    def pending_tasks(self) -> set[asyncio.Task]:
        return set(self._pending)

    @staticmethod
    def _wants_extraction(node: Node) -> bool:
        spec = node.extraction
        return spec is not None and spec.enabled and bool(spec.variables)

    def extract(self, node: Node, history: list[ChatMessage]) -> None:
        """Queue only — start_deferred() runs the LLM after the spoken turn."""
        if not self._wants_extraction(node):
            return
        self._deferred.append((node, list(history)))

    def start_deferred(self) -> None:
        """Spawn queued extracts. Safe to call more than once."""
        while self._deferred:
            node, history = self._deferred.pop(0)
            task = asyncio.ensure_future(self._extract(node, history))
            self._pending.add(task)
            self._inflight[task] = (node, history)
            task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._pending.discard(task)
        self._inflight.pop(task, None)

    def interrupt_for_live_turn(self) -> None:
        """Caller spoke — free the LLM; re-queue in-flight extracts for after this turn."""
        for task in list(self._pending):
            item = self._inflight.pop(task, None)
            task.cancel()
            if item is not None:
                self._deferred.append(item)
        self._pending.clear()

    def cancel_pending(self) -> None:
        """Drop queued work and cancel in-flight tasks (hangup timeout)."""
        self._deferred.clear()
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        self._inflight.clear()

    async def extract_final(self, node: Node, history: list[ChatMessage]) -> None:
        """Idempotent hangup pass; no-ops on end nodes with no extraction."""
        if self._final_done or not self._wants_extraction(node):
            return
        self._final_done = True
        await self._extract(node, list(history))

    async def flush(self) -> None:
        """Await in-flight extracts (call end)."""
        pending = list(self._pending)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)

    async def _extract(self, node: Node, history: list[ChatMessage]) -> None:
        try:
            spec = node.extraction
            wanted = "\n".join(
                f"- {v.name} ({v.type}): {v.prompt}" for v in spec.variables
            )
            instruction = (
                "You are extracting structured data from a phone call transcript.\n"
                f"{spec.prompt.strip()}\n\n" if spec.prompt.strip() else
                "You are extracting structured data from a phone call transcript.\n\n"
            )
            prompt = (
                f"{instruction}"
                f"Transcript so far:\n{_transcript(history)}\n\n"
                f"Extract these values:\n{wanted}\n\n"
                "Reply with a single JSON object whose keys are exactly the names above. "
                "Use null for anything the caller did not actually say — never guess."
            )
            # System message suppresses the provider's voice prompt (build_chat_messages).
            raw = await _collect(
                self._llm,
                [
                    ChatMessage(role="system", content=_EXTRACT_SYSTEM),
                    ChatMessage(role="user", content=prompt),
                ],
                self._timeout_s,
            )
            parsed = _parse_json_object(raw)
            values = {
                v.name: _coerce(parsed.get(v.name), v.type)
                for v in spec.variables
                if parsed.get(v.name) is not None
            }
            values = {k: v for k, v in values.items() if v is not None}
            if values:
                log.info("workflow: extracted %s at node=%s", sorted(values), node.name)
                self._on_variables(values)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("workflow: variable extraction timed out node=%s", node.name)
        except Exception:
            log.exception("workflow: variable extraction failed node=%s", node.name)


class ContextSummarizer:
    """Queue on maybe_summarize(); start_deferred() after the live turn."""

    def __init__(
        self,
        llm: Any,
        threshold_msgs: int = _SUMMARY_THRESHOLD_MSGS,
        keep_last: int = _SUMMARY_KEEP_LAST,
        timeout_s: float = _SUMMARY_TIMEOUT_S,
    ) -> None:
        self._llm = llm
        self._threshold = threshold_msgs
        self._keep_last = keep_last
        self._timeout_s = timeout_s
        self._deferred: list[ChatMessage] | None = None
        self._task: asyncio.Task | None = None
        self._inflight_history: list[ChatMessage] | None = None

    def has_background_work(self) -> bool:
        return self._task is not None and not self._task.done()

    def maybe_summarize(self, history: list[ChatMessage]) -> None:
        if len(history) <= self._threshold:
            return
        # Latest transition wins if several fire before start_deferred.
        self._deferred = history

    def start_deferred(self) -> None:
        if self._deferred is None:
            return
        history = self._deferred
        self._deferred = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._inflight_history = history
        self._task = asyncio.ensure_future(self._summarize(history))
        self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if self._task is task:
            self._task = None
            self._inflight_history = None

    def interrupt_for_live_turn(self) -> None:
        """Caller spoke — free the LLM; re-queue an in-flight summary."""
        if self._task is not None and not self._task.done():
            hist = self._inflight_history
            self._task.cancel()
            self._task = None
            self._inflight_history = None
            if hist is not None and self._deferred is None:
                self._deferred = hist
        # Keep an already-queued _deferred for after this turn.

    def cancel(self) -> None:
        self._deferred = None
        self._inflight_history = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _summarize(self, history: list[ChatMessage]) -> None:
        try:
            cutoff = len(history) - self._keep_last
            # Do not leave a tool result without its assistant tool_calls parent.
            while cutoff < len(history) and history[cutoff].role == "tool":
                cutoff += 1
            if cutoff <= 1 or cutoff >= len(history):
                return
            to_replace = list(history[1:cutoff])
            older = _strip_transition_noise(to_replace)
            if not older:
                return
            prompt = (
                "Summarize this part of an ongoing phone call in a short paragraph. "
                "Keep every concrete fact the caller gave — names, numbers, dates, "
                "what they asked for, what was agreed. Do not add anything they "
                "did not say.\n\n"
                + "\n".join(f"{m.role}: {m.content.strip()}" for m in older if (m.content or "").strip())
            )
            summary = (await _collect(
                self._llm,
                [
                    ChatMessage(role="system", content=_SUMMARY_SYSTEM),
                    ChatMessage(role="user", content=prompt),
                ],
                self._timeout_s,
            )).strip()
            if not summary:
                return

            # Identity splice: trim/append may have shifted indexes while we waited.
            if not history or history[0].role != "system":
                return
            drop_ids = {id(m) for m in to_replace}
            if not any(id(m) in drop_ids for m in history[1:]):
                return
            kept = [m for m in history[1:] if id(m) not in drop_ids]
            while kept and kept[0].role == "tool":
                kept.pop(0)
            history[1:] = [
                ChatMessage(role="user", content=f"[Earlier in this call]\n{summary}"),
                *kept,
            ]
            log.info("workflow: summarized %d earlier messages into context", len(to_replace))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("workflow: context summarization timed out — keeping full context")
        except Exception:
            log.exception("workflow: context summarization failed — keeping full context")
