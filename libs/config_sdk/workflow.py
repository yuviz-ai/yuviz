"""
Shared workflow graph model + validation (docs/workflow.md §5.1).

Config validates on publish; conversation walks the same object at runtime.
Dataclass-only (no pydantic) to match the rest of this SDK.

parse_graph() raises on runtime-breaking rules; graph_warnings() reports
editor mistakes that should not block publish. Cycles are allowed — loops
back to Q&A are valid; runaway calls are bounded by max_call_duration_s.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

NODE_TYPES = ("start", "agent", "transfer", "end", "global")
TERMINAL_NODE_TYPES = ("end", "transfer")
# `global` is always-on prompt text, not a call step — exempt from wiring rules.
UNWIRED_NODE_TYPES = ("global",)

# Platform-recognized disposition codes. End nodes may use any string;
# tenants invent codes freely. ENDED_EARLY is written by the runtime when
# the agent hangs up outside an end node (missing edge), never by the editor.
ENDED_EARLY = "ended_early"
SYSTEM_DISPOSITIONS = (
    "completed", "qualified", "not_qualified", "transferred", "abandoned", "failed",
    ENDED_EARLY,
)

# Always supplied by the pipeline per call. Other {{ vars }} must come from
# extraction config or campaign contact data, or graph_warnings() flags them.
CALL_CONTEXT_VARIABLES = (
    "caller_number", "called_number", "direction", "agent_name",
    "current_date", "current_time", "business_name",
)

# Deliberately not Jinja2: templates include caller-influenced values, and
# full Jinja would be a sandbox-escape surface with no upside here.
# Valid: {{ name }} / {{ name | fallback }}. A second pattern catches any
# other {{ ... }} (closing braces optional) so typos like {{ name } never
# reach TTS. [^}]* (not [^{}]*) so a valid fallback may contain `{`.
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|([^}]*))?\}\}")
_ANY_BRACES_RE = re.compile(r"\{\{[^}]*\}{0,2}")


@dataclass(frozen=True)
class WorkflowError:
    """Editor-facing problem: paint this node/edge red. Messages use UI words
    (step, connection, agent), not implementation jargon."""
    kind:    str          # "node" | "edge" | "workflow"
    id:      str | None
    field:   str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "field": self.field, "message": self.message}


class WorkflowInvalid(Exception):
    def __init__(self, errors: list[WorkflowError]) -> None:
        self.errors = errors
        super().__init__("; ".join(e.message for e in errors))


@dataclass(frozen=True)
class ExtractionVariable:
    name:   str
    type:   str = "string"   # string | number | boolean
    prompt: str = ""


@dataclass(frozen=True)
class Extraction:
    enabled:   bool = False
    prompt:    str = ""
    variables: tuple[ExtractionVariable, ...] = ()


@dataclass(frozen=True)
class Edge:
    id:                str
    source:            str
    target:            str
    label:             str
    condition:         str
    transition_speech: str | None = None

    @property
    def tool_name(self) -> str:
        # Labels that collapse to the same name ("yes" / "Yes!") are rejected
        # at validation — the second would silently shadow the first.
        return re.sub(r"[^a-z0-9]+", "_", self.label.lower()).strip("_")


@dataclass
class Node:
    id:                   str
    type:                 str
    name:                 str
    prompt:               str = ""
    greeting:             str | None = None
    delayed_start_ms:     int = 0
    tools:                list[str] = field(default_factory=list)
    knowledge_base_ids:   list[str] = field(default_factory=list)
    extraction:           Extraction | None = None
    transfer_destination: str | None = None
    disposition:          str | None = None
    out_edges:            list[Edge] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_NODE_TYPES

    @property
    def is_unwired(self) -> bool:
        return self.type in UNWIRED_NODE_TYPES


@dataclass
class WorkflowGraph:
    nodes:         dict[str, Node]
    start_node_id: str

    @property
    def start(self) -> Node:
        return self.nodes[self.start_node_id]

    @property
    def is_single_stage(self) -> bool:
        """Starter/backfill shape (start+end only). Multi-node authored graphs are False."""
        return all(
            n.type in ("start", "end") or n.is_unwired
            for n in self.nodes.values()
        )

    @property
    def global_prompt(self) -> str:
        """Always-on instruction prepended to every step. Empty if none."""
        for node in self.nodes.values():
            if node.is_unwired:
                return node.prompt
        return ""

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = [self.start_node_id]
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend(e.target for e in self.nodes[node_id].out_edges)
        return seen

    def template_variables(self) -> set[str]:
        found: set[str] = set()
        for node in self.nodes.values():
            for text in (node.prompt, node.greeting or ""):
                found |= template_variables(text)
            for edge in node.out_edges:
                found |= template_variables(edge.transition_speech or "")
        return found

    def declared_variables(self) -> set[str]:
        """Flat set of names any enabled extraction may produce (order ignored).
        Prefer `_variables_available_at` / `graph_warnings` for render-time checks."""
        return {
            v.name
            for node in self.nodes.values()
            if node.extraction is not None and node.extraction.enabled
            for v in node.extraction.variables
        }

    def _declared_by(self) -> dict[str, set[str]]:
        return {
            node.id: (
                {v.name for v in node.extraction.variables}
                if node.extraction is not None and node.extraction.enabled
                else set()
            )
            for node in self.nodes.values()
        }

    def _incoming(self) -> dict[str, list[str]]:
        incoming: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for node in self.nodes.values():
            for edge in node.out_edges:
                incoming[edge.target].append(edge.source)
        return incoming

    def _variables_available_at(self, node_id: str, *, leaving: bool = False) -> set[str]:
        """Vars guaranteed on *every* path from start to this node.

        Must-def dataflow (not "any ancestor"): a skip-branch that bypasses
        the capturing step must not clear the warning. Extraction runs when
        leaving a step, so `leaving=True` also includes this node's own vars
        (for transition_speech).
        """
        declared = self._declared_by()
        reachable = self.reachable()
        if node_id not in reachable:
            return set()

        incoming = self._incoming()
        universe: set[str] = set().union(*(declared.values())) if declared else set()

        # available_in[n] = vars guaranteed when entering n.
        available_in: dict[str, set[str]] = {
            nid: set(universe) for nid in reachable
        }
        available_in[self.start_node_id] = set()

        changed = True
        while changed:
            changed = False
            for nid in reachable:
                if nid == self.start_node_id:
                    continue
                preds = [p for p in incoming[nid] if p in reachable]
                if not preds:
                    new_in: set[str] = set()
                else:
                    new_in = None  # type: ignore[assignment]
                    for pred in preds:
                        out_pred = available_in[pred] | declared.get(pred, set())
                        new_in = out_pred if new_in is None else (new_in & out_pred)
                    new_in = new_in or set()
                if new_in != available_in[nid]:
                    available_in[nid] = new_in
                    changed = True

        result = set(available_in[node_id])
        if leaving:
            result |= declared.get(node_id, set())
        return result


def template_variables(text: str) -> set[str]:
    return {m.group(1) for m in _TEMPLATE_RE.finditer(text or "")}


def malformed_templates(text: str) -> list[str]:
    """`{{ ... }}` chunks that are not a valid {{ name }} / {{ name | fallback }}."""
    found: list[str] = []
    for m in _ANY_BRACES_RE.finditer(text or ""):
        chunk = m.group(0)
        if not _TEMPLATE_RE.fullmatch(chunk):
            found.append(chunk)
    return found


def render(text: str, variables: dict[str, Any]) -> str:
    """Substitute {{ name }} / {{ name | fallback }}. Unknown or malformed
    placeholders never leave literal `{{ ... }}` in speech (TTS/recordings)."""
    if not text:
        return text or ""

    def _sub(m: re.Match[str]) -> str:
        value = variables.get(m.group(1))
        if value is None or value == "":
            return (m.group(2) or "").strip()
        return str(value)

    text = _TEMPLATE_RE.sub(_sub, text)
    # Neutralize anything still matching {{ ... }} (e.g. {{123}}, {{ }}).
    return _ANY_BRACES_RE.sub("", text)


def _data_object(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data")
    return data if isinstance(data, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_extraction(raw: Any) -> Extraction | None:
    if not isinstance(raw, dict):
        return None
    raw_vars = raw.get("variables") or []
    if not isinstance(raw_vars, list):
        raw_vars = []
    variables = tuple(
        ExtractionVariable(
            name=str(v.get("name") or "").strip(),
            type=str(v.get("type") or "string"),
            prompt=str(v.get("prompt") or ""),
        )
        for v in raw_vars
        if isinstance(v, dict) and str(v.get("name") or "").strip()
    )
    return Extraction(
        enabled=bool(raw.get("enabled", False)),
        prompt=str(raw.get("prompt") or ""),
        variables=variables,
    )


def _str_list(raw: Any) -> list[str]:
    # Reject non-lists (e.g. a string would iterate character-by-character).
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


# Mistyped delays (30000 vs 300) must not hold the line silent for half a minute.
_MAX_DELAYED_START_MS = 2_000


def _delayed_start_ms(raw: Any) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if value > _MAX_DELAYED_START_MS:
        return _MAX_DELAYED_START_MS
    return value


def _node_from_raw(raw: dict[str, Any]) -> Node:
    data = _data_object(raw)
    greeting = data.get("greeting")
    return Node(
        id=str(raw.get("id") or ""),
        type=str(raw.get("type") or ""),
        name=str(data.get("name") or "").strip(),
        prompt=str(data.get("prompt") or ""),
        greeting=None if greeting is None else str(greeting),
        delayed_start_ms=_delayed_start_ms(data.get("delayed_start_ms")),
        tools=_str_list(data.get("tools")),
        knowledge_base_ids=_str_list(data.get("knowledge_base_ids")),
        extraction=_parse_extraction(data.get("extraction")),
        transfer_destination=_optional_str(data.get("transfer_destination")),
        disposition=_optional_str(data.get("disposition")),
    )


def _edge_from_raw(raw: dict[str, Any]) -> Edge:
    data = _data_object(raw)
    speech = str(data.get("transition_speech") or "").strip() or None
    return Edge(
        id=str(raw.get("id") or ""),
        source=str(raw.get("source") or ""),
        target=str(raw.get("target") or ""),
        label=str(data.get("label") or "").strip(),
        condition=str(data.get("condition") or "").strip(),
        transition_speech=speech,
    )


def parse_graph(raw: dict[str, Any]) -> WorkflowGraph:
    """Raises WorkflowInvalid — never returns a partial graph."""
    errors, graph = _validate_and_build(raw)
    if errors:
        raise WorkflowInvalid(errors)
    assert graph is not None
    return graph


def _structural_errors(raw: Any) -> list[WorkflowError]:
    """Every rule here is a runtime break (docs/workflow.md §5.1)."""
    errors, _ = _validate_and_build(raw)
    return errors


def _validate_and_build(
    raw: Any,
) -> tuple[list[WorkflowError], WorkflowGraph | None]:
    """Validate once and reuse the coerced nodes/edges — no second parse."""
    errors: list[WorkflowError] = []

    def err(kind: str, id_: str | None, field_: str | None, message: str) -> None:
        errors.append(WorkflowError(kind=kind, id=id_, field=field_, message=message))

    if not isinstance(raw, dict):
        return [WorkflowError("workflow", None, None, "workflow must be an object")], None

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return [WorkflowError("workflow", None, "nodes", "workflow has no nodes")], None
    raw_edges = raw.get("edges") or []
    if not isinstance(raw_edges, list):
        return [WorkflowError("workflow", None, "edges", "edges must be a list")], None

    nodes: dict[str, Node] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            err("workflow", None, "nodes", "every step must be an object")
            continue
        node = _node_from_raw(raw_node)
        if not node.id:
            err("node", None, "id", "A step is missing its id — try reloading the page")
            continue
        if node.id in nodes:
            err("node", node.id, "id", f"Two steps share the id {node.id!r} — try reloading the page")
            continue
        if node.type not in NODE_TYPES:
            err("node", node.id, "type", f"{node.type!r} isn't a kind of step this editor knows about")
        if not node.name:
            err("node", node.id, "name", "This step needs a name — it's what you'll see in your call logs")
        nodes[node.id] = node
    if errors:
        return errors, None

    # Names appear in logs/transcripts — duplicates make analytics unusable.
    seen_names: dict[str, str] = {}
    for node in nodes.values():
        clash = seen_names.get(node.name.lower())
        if clash is not None:
            err("node", node.id, "name",
                f"Another step is already called {node.name!r}. Names show up in your call "
                "logs and transcripts, so each one has to be different")
        seen_names[node.name.lower()] = node.id

    starts = [n for n in nodes.values() if n.type == "start"]
    if len(starts) != 1:
        err("workflow", None, None,
            "A flow needs exactly one starting point — "
            + ("there isn't one" if not starts else f"there are {len(starts)}"))
    if not any(n.type == "end" for n in nodes.values()):
        err("workflow", None, None,
            "Add a step that ends the call, otherwise the conversation has no way to finish")
    # Multiple globals would concatenate in storage order — a prompt nobody wrote.
    globals_ = [n for n in nodes.values() if n.is_unwired]
    if len(globals_) > 1:
        err("workflow", None, None,
            f"There are {len(globals_)} always-on instructions — keep one, and put anything "
            "step-specific on the step itself")

    edges: list[Edge] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            err("workflow", None, "edges", "every connection must be an object")
            continue
        edge = _edge_from_raw(raw_edge)
        if edge.source not in nodes:
            err("edge", edge.id, "source",
                f"This connection points from unknown step {edge.source!r} — remove it or fix the id")
            continue
        if edge.target not in nodes:
            err("edge", edge.id, "target",
                f"This connection points to unknown step {edge.target!r} — remove it or fix the id")
            continue
        # Brand-new connections lack both fields — one message, not two.
        if not edge.label and not edge.condition:
            err("edge", edge.id, "condition",
                "This connection isn't set up yet — it needs a short name and a description of "
                "when the call should take it")
        else:
            if not edge.label:
                err("edge", edge.id, "label",
                    "This connection needs a short name — it's how the agent refers to this move")
            elif not edge.tool_name:
                err("edge", edge.id, "label",
                    f"{edge.label!r} has no letters or numbers in it, so it can't be used as a name")
            if not edge.condition:
                err("edge", edge.id, "condition",
                    "This connection needs a condition. Without one the agent never knows when to "
                    "take it, so the call can't move on")
        edges.append(edge)

    out_edges: dict[str, list[Edge]] = {node_id: [] for node_id in nodes}
    in_count: dict[str, int] = {node_id: 0 for node_id in nodes}
    for edge in edges:
        out_edges[edge.source].append(edge)
        in_count[edge.target] += 1

    for node in nodes.values():
        if node.is_unwired:
            if in_count[node.id] or out_edges[node.id]:
                err("node", node.id, None,
                    f"{node.name!r} applies to every step, so it isn't part of the flow — "
                    "remove the connection")
            continue
        if node.type == "start" and in_count[node.id]:
            err("node", node.id, None,
                "Nothing can lead back into the starting point — it's where every call begins")
        if node.type == "transfer" and not str(node.transfer_destination or "").strip():
            err("node", node.id, "transfer_destination",
                f"{node.name!r} hands the call to a human, so it needs a destination number")
        if node.is_terminal and out_edges[node.id]:
            err("node", node.id, None,
                f"{node.name!r} " + ("hands the call to a human" if node.type == "transfer" else "ends the call")
                + ", so nothing can lead out of it — remove the connection leaving it")
        if not node.is_terminal and not out_edges[node.id]:
            err("node", node.id, None,
                f"{node.name!r} has no way out. A call that reaches it would be stuck there "
                "until it hits the time limit — connect it to another step, or make it end the call")
        # "yes" / "Yes!" both become `yes` — second schema silently shadows the first.
        by_tool_name: dict[str, Edge] = {}
        for edge in out_edges[node.id]:
            if not edge.tool_name:
                continue
            clash = by_tool_name.get(edge.tool_name)
            if clash is not None:
                err("edge", edge.id, "label",
                    f"{edge.label!r} and {clash.label!r} look like the same move to the agent, so "
                    "only one of them would ever fire — give one a different name")
            by_tool_name[edge.tool_name] = edge

    if errors:
        return errors, None

    for node in nodes.values():
        node.out_edges = list(out_edges[node.id])
    start_id = next(n.id for n in nodes.values() if n.type == "start")
    return [], WorkflowGraph(nodes=nodes, start_node_id=start_id)


def _warn_templates(
    warnings: list[WorkflowError],
    *,
    kind: str,
    id_: str,
    field_name: str,
    text: str,
    available: set[str],
    declared_anywhere: set[str],
    node_name: str | None = None,
    always_on: bool = False,
) -> None:
    for chunk in malformed_templates(text):
        warnings.append(WorkflowError(
            kind, id_, field_name,
            f"{chunk} isn't a valid placeholder — use {{{{ name }}}} "
            "(letters/numbers/underscore only)",
        ))
    for name in sorted(template_variables(text) - available):
        if always_on:
            msg = (
                f"{{{{ {name} }}}} is in the always-on instruction, so it has to come from "
                "the call itself (or contact data) — a later step can't fill it in time"
            )
        elif name in declared_anywhere:
            where = f"on {node_name!r}" if node_name else "on this connection"
            msg = (
                f"{{{{ {name} }}}} is used {where} before every path captures it, "
                "so it will come out blank when the agent speaks"
            )
        else:
            msg = (
                f"Nothing in this flow provides {{{{ {name} }}}}, so it will come out "
                "blank when the agent speaks. Check the spelling, or have a step capture it"
            )
        warnings.append(WorkflowError(kind, id_, field_name, msg))


def graph_warnings(graph: WorkflowGraph, known_variables: Iterable[str] = ()) -> list[WorkflowError]:
    """Non-blocking editor mistakes: unreachable steps, {{ vars }} not filled
    on every path before they are spoken (extraction runs when leaving a step)."""
    warnings: list[WorkflowError] = []
    reachable = graph.reachable()
    always = set(CALL_CONTEXT_VARIABLES) | set(known_variables)
    declared_anywhere = graph.declared_variables()

    for node in graph.nodes.values():
        if node.is_unwired:
            _warn_templates(
                warnings, kind="node", id_=node.id, field_name="prompt",
                text=node.prompt, available=always, declared_anywhere=declared_anywhere,
                node_name=node.name, always_on=True,
            )
            continue
        if node.id not in reachable:
            warnings.append(WorkflowError(
                "node", node.id, None,
                f"No path leads to {node.name!r}, so no call will ever reach it",
            ))
            continue

        on_entry = always | graph._variables_available_at(node.id, leaving=False)
        for field_name, text in (("prompt", node.prompt), ("greeting", node.greeting or "")):
            _warn_templates(
                warnings, kind="node", id_=node.id, field_name=field_name,
                text=text, available=on_entry, declared_anywhere=declared_anywhere,
                node_name=node.name,
            )

        on_leave = always | graph._variables_available_at(node.id, leaving=True)
        for edge in node.out_edges:
            _warn_templates(
                warnings, kind="edge", id_=edge.id, field_name="transition_speech",
                text=edge.transition_speech or "", available=on_leave,
                declared_anywhere=declared_anywhere,
            )

    return warnings


# React Flow editor chrome — not conversation logic.
_RF_NODE_LOGIC = ("id", "type", "data")
_RF_EDGE_LOGIC = ("id", "source", "target", "sourceHandle", "targetHandle", "data")


def graphs_equivalent(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """True when graphs match on conversation logic, ignoring RF chrome/order."""
    if a is None or b is None:
        return a is b
    return _logic_view(a) == _logic_view(b)


def _logic_view(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = [
        {k: n[k] for k in _RF_NODE_LOGIC if k in n}
        for n in (graph.get("nodes") or [])
    ]
    edges = [
        {k: e[k] for k in _RF_EDGE_LOGIC if k in e}
        for e in (graph.get("edges") or [])
    ]
    nodes.sort(key=lambda n: str(n.get("id", "")))
    edges.sort(key=lambda e: str(e.get("id", "")))
    return {
        "version": graph.get("version"),
        "nodes": nodes,
        "edges": edges,
    }


def starter_graph(
    greeting: str = "",
    system_prompt: str = "",
    tools: list[str] | None = None,
    knowledge_base_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Default React Flow JSON for a new agent: global + start + end.

    `tools` / `knowledge_base_ids` go on start (the only non-terminal).
    Both are default-deny — migrations must pass the agent's existing
    tool policies and agent_knowledge_bases rows or booking/RAG vanish.
    """
    return {
        "version": 1,
        "nodes": [
            {
                "id": "global", "type": "global", "position": {"x": 330, "y": 0},
                "data": {"name": "always applies", "prompt": system_prompt},
            },
            {
                "id": "start", "type": "start", "position": {"x": 0, "y": 0},
                "data": {
                    "name": "greeting",
                    "prompt": "Greet the caller and find out what they need.",
                    "greeting": greeting,
                    "tools": list(tools or []),
                    "knowledge_base_ids": list(knowledge_base_ids or []),
                },
            },
            {
                "id": "end", "type": "end", "position": {"x": 0, "y": 230},
                "data": {
                    "name": "goodbye",
                    "prompt": "Confirm anything outstanding and close warmly.",
                    "disposition": "completed",
                },
            },
        ],
        "edges": [
            {
                "id": "e-start-end", "source": "start", "target": "end",
                "data": {
                    "label": "conversation finished",
                    "condition": "The caller has no further questions.",
                },
            },
        ],
    }
