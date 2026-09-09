"""
WorkflowRunner — which node is active. No audio/providers (dry-run friendly).

Outgoing edges are local LLM tools; calling one advances the node.
Constructed per call with the handler — plain attrs, no session map.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from libs.config_sdk import RuntimeConfig
from libs.config_sdk.workflow import (
    ENDED_EARLY, Edge, Node, WorkflowGraph, WorkflowInvalid, parse_graph, render,
    starter_graph,
)

from ..providers.interfaces import ChatMessage
from ..tools.types import ToolDefinition, ToolResult, ToolStatus

log = logging.getLogger(__name__)

_NO_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}
_TRANSITION_RESULT = ToolResult(status=ToolStatus.SUCCESS, payload={"status": "done"})

# One entry per agent; replaced when config_version changes. Cap bounds fleet size.
_GRAPH_CACHE_MAX = 256
_GRAPH_CACHE: dict[tuple[str, str], tuple[int, WorkflowGraph]] = {}


def _transition_tool_name(edge: Edge) -> str:
    """Prefix so edge labels cannot shadow ToolRegistry names (book_appointment…)."""
    return f"goto_{edge.tool_name}"


class WorkflowRunner:
    def __init__(
        self,
        graph: WorkflowGraph,
        *,
        base_suffix: str = "",
        default_global: str = "",
        variables: dict[str, Any] | None = None,
        extractor: Any | None = None,
        summarizer: Any | None = None,
    ) -> None:
        self._graph = graph
        self._node = graph.start
        self._global = graph.global_prompt
        self._default_global = (default_global or "").strip()
        self._suffix = base_suffix
        self._vars: dict[str, Any] = dict(variables or {})
        self._extractor = extractor
        self._summarizer = summarizer
        self.visited: list[str] = [self._node.name]
        # Pipeline reads/clears these after each turn (speech / hangup / transfer).
        self.pending_speech: str | None = None
        self.pending_end: bool = False
        self.pending_transfer: Node | None = None
        self.ended_off_graph: bool = False
        self.last_transition: str = ""
        self._pre_transfer_node: Node | None = None

    @property
    def node(self) -> Node:
        return self._node

    @property
    def variables(self) -> dict[str, Any]:
        return dict(self._vars)

    def update_variables(self, values: dict[str, Any]) -> None:
        self._vars.update({k: v for k, v in values.items() if v is not None})

    def extracted_variables(self) -> dict[str, Any]:
        """Values produced by extraction only — not seeded call-context keys."""
        declared = self._graph.declared_variables()
        return {k: v for k, v in self._vars.items() if k in declared}

    def system_prompt(self) -> str:
        global_part = self.render(self._global).strip() or self._default_global
        parts = [
            global_part,
            self.render(self._node.prompt),
            self._suffix,
        ]
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    def allowed_tool_names(self) -> list[str] | None:
        """Node tool allow-list for ToolPolicyResolver `only`.

        None = do not narrow (starter/backfill — agent_tool_policies win).
        [] = deny every DB tool on this authored node.
        """
        if self._graph.is_single_stage:
            return None
        return list(self._node.tools)

    def knowledge_enabled(self) -> bool:
        """RAG on/off for this node.

        Single-stage / all-empty graphs keep agent_knowledge_bases RAG.
        Authored multi-node: non-empty ids → on; empty when another node
        opted in → off (per-stage).
        """
        if self._node.knowledge_base_ids:
            return True
        if self._graph.is_single_stage:
            return True
        return not any(n.knowledge_base_ids for n in self._graph.nodes.values())

    def greeting(self) -> str | None:
        text = self.render(self._graph.start.greeting or "")
        return text or None

    @property
    def delayed_start_ms(self) -> int:
        return self._graph.start.delayed_start_ms

    @property
    def disposition(self) -> str | None:
        """End-node code, or ENDED_EARLY if [[END_CALL]] left a non-terminal node."""
        if self.ended_off_graph and not self._node.is_terminal:
            return ENDED_EARLY
        return self._node.disposition

    def render(self, text: str) -> str:
        return render(text, self._vars)

    def local_tools(
        self,
        turn: list[ChatMessage] | None = None,
        store: list[ChatMessage] | None = None,
    ) -> dict[str, tuple[ToolDefinition, Callable[[dict[str, Any]], Awaitable[ToolResult]]]]:
        """One local tool per outgoing edge. `turn`/`store` may differ when RAG
        built a throwaway copy — prompt swap must hit both; extract/summarize only `store`."""
        tools: dict[str, tuple[ToolDefinition, Callable[..., Awaitable[ToolResult]]]] = {}
        for edge in self._node.out_edges:
            name = _transition_tool_name(edge)
            definition = ToolDefinition(
                name=name,
                description=edge.condition,
                parameters_schema=_NO_PARAMETERS,
                category="workflow_transition",
            )

            def handler(_args: dict[str, Any], _edge: Edge = edge) -> Awaitable[ToolResult]:
                return self._transition(_edge, turn, store if store is not None else turn)

            tools[name] = (definition, handler)
        return tools

    def abandon_transfer(self) -> None:
        """Move off a rejected transfer node so the caller is not dead-ended."""
        if self._pre_transfer_node is None or self._node.type != "transfer":
            self.pending_transfer = None
            return
        source = self._pre_transfer_node
        self._node = source
        self._pre_transfer_node = None
        self.pending_transfer = None
        if self.visited and self.visited[-1] != source.name:
            self.visited.pop()
        log.info("workflow: transfer rejected — reverted to %s", source.name)

    async def _transition(
        self,
        edge: Edge,
        turn: list[ChatMessage] | None,
        store: list[ChatMessage] | None,
    ) -> ToolResult:
        # Queue extract/summarize only — pipeline starts them after live generate.
        source = self._node

        if self._extractor is not None and source.extraction is not None and source.extraction.enabled:
            self._extractor.extract(source, store or [])

        self.pending_speech = self.render(edge.transition_speech or "") or None
        self._node = self._graph.nodes[edge.target]
        self.last_transition = edge.tool_name
        self.visited.append(self._node.name)
        log.info(
            "workflow: %s --%s--> %s", source.name, edge.tool_name, self._node.name,
        )

        if self._node.type == "end":
            self.pending_end = True
            self._pre_transfer_node = None
        elif self._node.type == "transfer":
            self.pending_transfer = self._node
            self._pre_transfer_node = source
        else:
            self._pre_transfer_node = None

        # Swap prompt mid-turn so the rest of this generate uses the new node.
        prompt: str | None = None
        for messages in (turn, store):
            if not messages:
                continue
            if prompt is None:
                prompt = self.system_prompt()
            if messages[0].role == "system":
                messages[0] = ChatMessage(role="system", content=prompt)
            else:
                messages.insert(0, ChatMessage(role="system", content=prompt))
            if messages is turn and store is turn:
                break

        if store and self._summarizer is not None:
            self._summarizer.maybe_summarize(store)

        return _TRANSITION_RESULT


def _cache_key(runtime_config: RuntimeConfig) -> tuple[str, str]:
    agent = runtime_config.agent.id or runtime_config.agent.slug or ""
    tenant = runtime_config.tenant.id or runtime_config.tenant.slug or ""
    return (tenant, agent)


def _cache_get(runtime_config: RuntimeConfig) -> WorkflowGraph | None:
    key = _cache_key(runtime_config)
    item = _GRAPH_CACHE.get(key)
    if item is None:
        return None
    version, graph = item
    if version != runtime_config.version:
        return None
    return graph


def _cache_put(runtime_config: RuntimeConfig, graph: WorkflowGraph) -> None:
    key = _cache_key(runtime_config)
    if key not in _GRAPH_CACHE and len(_GRAPH_CACHE) >= _GRAPH_CACHE_MAX:
        # Drop an arbitrary entry — only current versions are retained per agent.
        _GRAPH_CACHE.pop(next(iter(_GRAPH_CACHE)))
    _GRAPH_CACHE[key] = (runtime_config.version, graph)


def _str_names_from_raw(raw: dict[str, Any] | None, field: str) -> list[str]:
    """Best-effort scrape of node list fields from unparseable published JSON."""
    if not isinstance(raw, dict):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for node in raw.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        for item in data.get(field) or []:
            if isinstance(item, str) and item and item not in seen:
                seen.add(item)
                names.append(item)
    return names


def graph_for(runtime_config: RuntimeConfig, *, draft: bool = False) -> WorkflowGraph:
    """Never raises. Missing/bad published graph → starter seeded from column
    greeting/system_prompt (until PR11 drops those columns).

    draft=True prefers workflow_draft; invalid draft falls back to published.
    Callers that want draft testing must pass draft=True (admin test-call path;
    not wired on SessionOpenRequest yet — PR10).
    """
    raw = runtime_config.conversation.workflow
    if draft and runtime_config.conversation.workflow_draft:
        raw = runtime_config.conversation.workflow_draft
    if not raw:
        if not draft:
            log.error(
                "workflow: agent %s has no published graph — running starter "
                "seeded from greeting/system_prompt",
                runtime_config.agent.slug,
            )
        return _fallback_graph(runtime_config, raw=None)
    if not draft:
        cached = _cache_get(runtime_config)
        if cached is not None:
            return cached
    try:
        graph = parse_graph(raw)
    except WorkflowInvalid as exc:
        if draft:
            log.info(
                "workflow: draft for agent %s does not parse (%s) — using published",
                runtime_config.agent.slug, exc,
            )
            return graph_for(runtime_config, draft=False)
        log.error(
            "workflow: agent %s published graph does not parse (%s) — starter fallback",
            runtime_config.agent.slug, exc,
        )
        graph = _fallback_graph(runtime_config, raw=raw if isinstance(raw, dict) else None)
    except Exception:
        log.exception("workflow: unexpected parse failure for agent %s", runtime_config.agent.slug)
        graph = _fallback_graph(runtime_config, raw=raw if isinstance(raw, dict) else None)
    if not draft:
        _cache_put(runtime_config, graph)
    return graph


def _fallback_graph(
    runtime_config: RuntimeConfig, *, raw: dict[str, Any] | None,
) -> WorkflowGraph:
    # Prefer RuntimeConfig.tools; scrape broken published JSON so a parse
    # failure does not silently strip booking/SMS (Node.tools is default-deny).
    tools = [t.name for t in runtime_config.tools] or _str_names_from_raw(raw, "tools")
    kb_ids = _str_names_from_raw(raw, "knowledge_base_ids")
    return parse_graph(starter_graph(
        runtime_config.conversation.greeting or "",
        runtime_config.conversation.system_prompt or "",
        tools,
        kb_ids,
    ))
