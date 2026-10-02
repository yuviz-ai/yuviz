"""CallFlowRunner: pure call-flow state machine. Digits/timeouts in, Actions out;
no audio, timers or I/O."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from libs.config_sdk.callflow import (
    DTMF_KEYS, MAX_NODES, MENU_INVALID, MENU_TIMEOUT,
    CallFlowGraph, CallFlowNode, parse_graph,
)
from libs.config_sdk.models import CallFlow
from libs.config_sdk.workflow import render

log = logging.getLogger(__name__)

# One entry per (tenant, flow); replaced when config_version changes.
_FLOW_CACHE_MAX = 256
_FLOW_CACHE: dict[tuple[str, str], tuple[int, CallFlowGraph]] = {}


def graph_for_flow(flow: CallFlow) -> CallFlowGraph:
    """Cache-aside parse versioned by config_version. Raises CallFlowValidationError."""
    key = (flow.tenant_slug, flow.id)
    cached = _FLOW_CACHE.get(key)
    if cached is not None and cached[0] == flow.config_version:
        return cached[1]

    graph = parse_graph(flow.graph)
    if key not in _FLOW_CACHE and len(_FLOW_CACHE) >= _FLOW_CACHE_MAX:
        _FLOW_CACHE.pop(next(iter(_FLOW_CACHE)))
    _FLOW_CACHE[key] = (flow.config_version, graph)
    return graph


@dataclass(frozen=True)
class Speak:
    text: str


@dataclass(frozen=True)
class SetVoice:
    tts_config_id: str | None


@dataclass(frozen=True)
class Listen:
    timeout_ms: int
    digits_expected: bool


@dataclass(frozen=True)
class Store:
    name: str
    value: str
    sensitive: bool = False


@dataclass(frozen=True)
class Handoff:
    agent_id: str


@dataclass(frozen=True)
class Dial:
    destination: str


@dataclass(frozen=True)
class Hangup:
    reason: str  # "flow_complete" | "retries_exhausted" | "flow_error" | "flow_budget"


Action = Speak | SetVoice | Listen | Store | Handoff | Dial | Hangup


class CallFlowRunner:
    """One caller's walk through a shared, never-mutated CallFlowGraph.

    tts_config_id must come from CallFlow.resolved_tts_config_id, never from graph
    nodes: those are unvalidated and could name another tenant's TTS config.
    """

    def __init__(
        self,
        graph: CallFlowGraph,
        *,
        tts_config_id: str | None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        self._graph = graph
        self._tts_config_id = tts_config_id
        self._vars: dict[str, Any] = dict(variables or {})
        # Sensitive collects: never logged or exposed via `variables`.
        self._sensitive_keys: set[str] = set()
        self._node: CallFlowNode = graph.start
        self._retries = 0
        self._buffer: list[str] = []
        self._transitions = 0
        self.visited: list[str] = []

    @property
    def node(self) -> CallFlowNode:
        return self._node

    @property
    def variables(self) -> dict[str, Any]:
        """Copy without sensitive keys; used for handoff initial_variables."""
        return {k: v for k, v in self._vars.items() if k not in self._sensitive_keys}

    @property
    def flow_variables(self) -> dict[str, Any]:
        """Copy including sensitive keys — in-flow prompt rendering only.
        Never handed to a delegate, never logged."""
        return dict(self._vars)

    def update_variables(self, values: dict[str, Any]) -> None:
        self._vars.update({k: v for k, v in values.items() if v is not None})

    def open(self) -> list[Action]:
        return [SetVoice(self._tts_config_id), *self._enter(self._graph.start)]

    def on_digit(self, digit: str) -> list[Action]:
        if self._node.type == "menu":
            return self._menu_digit(digit)
        if self._node.type == "collect":
            return self._collect_digit(digit)
        log.info("callflow: non-DTMF input ignored node=%s", self._node.id)
        return []

    def on_timeout(self) -> list[Action]:
        if self._node.type == "menu":
            return self._menu_fallback(MENU_TIMEOUT)
        if self._node.type == "collect":
            return self._collect_timeout()
        return []

    # ── Internal ─────────────────────────────────────────────────────────

    def _render(self, text: str) -> str:
        # Sensitive values may render inside the flow but never leave it.
        return render(text, self.flow_variables)

    def _store(self, name: str, value: str, sensitive: bool) -> None:
        self._vars[name] = value
        if sensitive:
            self._sensitive_keys.add(name)
        else:
            self._sensitive_keys.discard(name)

    def _enter(self, node: CallFlowNode) -> list[Action]:
        """Node-visit actions; resets retries and the digit buffer."""
        self._node = node
        self._retries = 0
        self._buffer = []
        self.visited.append(node.id)
        return self._node_actions(node)

    def _advance(self, node: CallFlowNode) -> list[Action]:
        self._transitions += 1
        if self._transitions > MAX_NODES:
            log.error("callflow: transition budget exceeded at node=%s", node.id)
            return [Hangup("flow_budget")]
        return self._enter(node)

    def _follow_single_edge(self, node: CallFlowNode) -> list[Action]:
        edge = node.out_edges[0]
        return self._advance(self._graph.nodes[edge.target])

    def _node_actions(self, node: CallFlowNode) -> list[Action]:
        if node.type == "start":
            return self._follow_single_edge(node)
        if node.type == "play":
            return [Speak(self._render(node.prompt)), *self._follow_single_edge(node)]
        if node.type == "menu":
            return [Speak(self._render(node.prompt)), Listen(node.timeout_ms, digits_expected=False)]
        if node.type == "collect":
            return [Speak(self._render(node.prompt)), Listen(node.timeout_ms, digits_expected=True)]
        if node.type == "dial":
            actions: list[Action] = []
            if node.prompt.strip():
                actions.append(Speak(self._render(node.prompt)))
            actions.append(Dial(node.destination or ""))
            return actions
        if node.type == "agent":
            return [Handoff(node.agent_id or "")]
        if node.type == "hangup":
            actions = []
            if node.prompt.strip():
                actions.append(Speak(self._render(node.prompt)))
            actions.append(Hangup("flow_complete"))
            return actions
        raise AssertionError(f"unreachable node type {node.type!r}")

    def _edge_for_key(self, node: CallFlowNode, key: str) -> Any:
        for edge in node.out_edges:
            if edge.key == key:
                return edge
        return None

    def _menu_digit(self, digit: str) -> list[Action]:
        if digit not in DTMF_KEYS:
            log.info("callflow: non-DTMF input ignored node=%s", self._node.id)
            return []
        edge = self._edge_for_key(self._node, digit)
        if edge is not None:
            return self._advance(self._graph.nodes[edge.target])
        return self._menu_fallback(MENU_INVALID)

    def _menu_fallback(self, key: str) -> list[Action]:
        """Take an explicit timeout/invalid edge if present, else count an invalid attempt."""
        edge = self._edge_for_key(self._node, key)
        if edge is not None:
            return self._advance(self._graph.nodes[edge.target])
        return self._invalid_attempt()

    def _invalid_attempt(self) -> list[Action]:
        """Replay the node, or hang up once retries are exhausted."""
        self._retries += 1
        # Otherwise a short first attempt's digits would prefix the retry.
        self._buffer = []
        if self._retries > self._node.max_retries:
            return [Hangup("retries_exhausted")]
        digits_expected = self._node.type == "collect"
        return [Speak(self._render(self._node.prompt)),
                Listen(self._node.timeout_ms, digits_expected=digits_expected)]

    def _submit_collect(self) -> list[Action]:
        node = self._node
        value = "".join(self._buffer)
        actions: list[Action] = []
        if node.variable:
            actions.append(Store(node.variable, value, node.sensitive))
            self._store(node.variable, value, node.sensitive)
        actions.extend(self._follow_single_edge(node))
        return actions

    def _collect_digit(self, digit: str) -> list[Action]:
        if digit not in DTMF_KEYS:
            log.info("callflow: non-DTMF input ignored node=%s", self._node.id)
            return []
        node = self._node
        # Blank terminator means none: submit on max_digits or at timeout.
        if node.terminator and digit == node.terminator:
            if len(self._buffer) >= node.min_digits:
                return self._submit_collect()
            return self._invalid_attempt()
        self._buffer.append(digit)
        if len(self._buffer) >= node.max_digits:
            return self._submit_collect()
        return []

    def _collect_timeout(self) -> list[Action]:
        if len(self._buffer) >= self._node.min_digits:
            return self._submit_collect()
        return self._invalid_attempt()
