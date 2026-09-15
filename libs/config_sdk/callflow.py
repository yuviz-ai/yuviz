"""
Call-flow (IVR/OBD) graph model + validation.

Deliberately a separate module from workflow.py rather than more node types
bolted onto it. workflow.py's graph is the *conversational* one: its edges
carry natural-language conditions that the conversation service compiles
into LLM tools at runtime (services/conversation/workflow/runner.py), and
its correctness is load-bearing on every live call today. An IVR flow
branches on a keypress, which is a deterministic, non-LLM decision — mixing
the two vocabularies in one validator would mean every rule here has to
reason about whether it is in LLM-land or DTMF-land.

The two models meet at exactly one node type: `agent`, which hands the call
from the IVR to a conversational agent (and from there, that agent's own
workflow graph takes over).

Same conventions as workflow.py: dataclasses, no pydantic; parse_graph()
raises on runtime-breaking rules; graph_warnings() reports authoring
mistakes that should not block publishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NODE_TYPES = ("start", "play", "menu", "collect", "dial", "agent", "hangup")

# Nodes the call cannot continue past inside the flow: `dial` hands the call
# to a destination, `agent` hands it to a conversational agent, `hangup`
# ends it. None of them may have outgoing edges.
TERMINAL_NODE_TYPES = ("dial", "agent", "hangup")

# What a caller can actually press. Keypad only — there is no "any key".
DTMF_KEYS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "#")

# Two non-digit branches every menu may define. They are not keys the caller
# presses; the runtime picks them when nothing valid arrives in time, or when
# a key with no branch is pressed.
MENU_TIMEOUT = "timeout"
MENU_INVALID = "invalid"
MENU_FALLBACK_KEYS = (MENU_TIMEOUT, MENU_INVALID)

MAX_NODES = 200
MAX_COLLECT_DIGITS = 32


class CallFlowValidationError(Exception):
    """Raised by parse_graph(). `.errors` is the editor-facing list."""

    def __init__(self, errors: list["CallFlowError"]) -> None:
        super().__init__(f"{len(errors)} problem(s) in this call flow")
        self.errors = errors


@dataclass(frozen=True)
class CallFlowError:
    """Editor-facing problem: paint this node/edge red. UI words (step,
    branch, keypress), not implementation jargon."""
    kind:    str          # "node" | "edge" | "flow"
    id:      str | None
    field:   str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "field": self.field, "message": self.message}


@dataclass
class CallFlowEdge:
    id:     str
    source: str
    target: str
    # Which keypress takes this branch. None on a non-menu node's single
    # outgoing edge ("just continue"); one of DTMF_KEYS or a
    # MENU_FALLBACK_KEYS value on a menu's branches.
    key:    str | None = None


@dataclass
class CallFlowNode:
    id:   str
    type: str
    name: str
    # What the caller hears. `play`/`menu`/`collect` speak it; the others
    # ignore it (a `dial` may still set it as a pre-transfer announcement).
    prompt: str = ""
    # menu / collect
    timeout_ms:   int = 5000
    max_retries:  int = 2
    # collect only
    variable:     str | None = None
    min_digits:   int = 1
    max_digits:   int = 10
    terminator:   str = "#"
    # dial only
    destination:  str | None = None
    # agent only — which conversational agent picks the call up
    agent_id:     str | None = None
    out_edges:    list[CallFlowEdge] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_NODE_TYPES

    @property
    def speaks(self) -> bool:
        return self.type in ("play", "menu", "collect")


@dataclass
class CallFlowGraph:
    nodes:         dict[str, CallFlowNode]
    start_node_id: str

    @property
    def start(self) -> CallFlowNode:
        return self.nodes[self.start_node_id]

    def to_dict(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for n in self.nodes.values():
            data: dict[str, Any] = {"name": n.name, "prompt": n.prompt}
            if n.type in ("menu", "collect"):
                data["timeout_ms"] = n.timeout_ms
                data["max_retries"] = n.max_retries
            if n.type == "collect":
                data.update(
                    variable=n.variable, min_digits=n.min_digits,
                    max_digits=n.max_digits, terminator=n.terminator,
                )
            if n.type == "dial":
                data["destination"] = n.destination
            if n.type == "agent":
                data["agent_id"] = n.agent_id
            nodes.append({"id": n.id, "type": n.type, "data": data})
            for e in n.out_edges:
                edge: dict[str, Any] = {"id": e.id, "source": e.source, "target": e.target}
                if e.key is not None:
                    edge["data"] = {"key": e.key}
                edges.append(edge)
        return {"version": 1, "nodes": nodes, "edges": edges}


def _node_errors(raw: dict[str, Any], node: CallFlowNode) -> list[CallFlowError]:
    errs: list[CallFlowError] = []
    if node.type == "dial" and not (node.destination or "").strip():
        errs.append(CallFlowError("node", node.id, "destination",
                                  "This transfer step needs a number or SIP address to dial."))
    if node.type == "agent" and not (node.agent_id or "").strip():
        errs.append(CallFlowError("node", node.id, "agent_id",
                                  "This step hands the call to an AI agent — pick which one."))
    if node.type == "collect":
        if not (node.variable or "").strip():
            errs.append(CallFlowError("node", node.id, "variable",
                                      "Name the value being collected, so later steps can use it."))
        if not 1 <= node.min_digits <= node.max_digits <= MAX_COLLECT_DIGITS:
            errs.append(CallFlowError(
                "node", node.id, "min_digits",
                f"Digit count must run from 1 to {MAX_COLLECT_DIGITS}, with the minimum "
                f"no larger than the maximum.",
            ))
    if node.speaks and not node.prompt.strip():
        errs.append(CallFlowError("node", node.id, "prompt",
                                  "This step speaks to the caller, so it needs something to say."))
    return errs


def _wiring_errors(graph_nodes: dict[str, CallFlowNode]) -> list[CallFlowError]:
    errs: list[CallFlowError] = []
    for node in graph_nodes.values():
        targets_ok = [e for e in node.out_edges if e.target in graph_nodes]
        for e in node.out_edges:
            if e.target not in graph_nodes:
                errs.append(CallFlowError("edge", e.id, "target",
                                          "This connection points at a step that no longer exists."))

        if node.is_terminal and node.out_edges:
            errs.append(CallFlowError(
                "node", node.id, None,
                "The call leaves the flow at this step, so it cannot lead anywhere else.",
            ))
            continue
        if node.is_terminal:
            continue

        if not targets_ok:
            errs.append(CallFlowError("node", node.id, None,
                                      "This step has no next step — the call would stall here."))
            continue

        if node.type == "menu":
            seen: set[str] = set()
            digit_branches = 0
            for e in targets_ok:
                if e.key is None:
                    errs.append(CallFlowError("edge", e.id, "key",
                                              "Every branch out of a menu needs a keypress."))
                    continue
                if e.key in seen:
                    errs.append(CallFlowError("edge", e.id, "key",
                                              f"Two branches both answer {e.key!r}."))
                seen.add(e.key)
                if e.key in DTMF_KEYS:
                    digit_branches += 1
                elif e.key not in MENU_FALLBACK_KEYS:
                    errs.append(CallFlowError(
                        "edge", e.id, "key",
                        f"{e.key!r} is not a key a caller can press.",
                    ))
            if digit_branches == 0:
                errs.append(CallFlowError("node", node.id, None,
                                          "A menu needs at least one keypress branch."))
        elif len(targets_ok) > 1:
            errs.append(CallFlowError(
                "node", node.id, None,
                "Only a menu can branch — this step must lead to exactly one next step.",
            ))
    return errs


def parse_graph(raw: dict[str, Any]) -> CallFlowGraph:
    """Raises CallFlowValidationError on anything that would break at call
    time. Authoring nits (unreachable steps, no hangup) are warnings."""
    errs: list[CallFlowError] = []
    raw_nodes = raw.get("nodes") or []
    raw_edges = raw.get("edges") or []

    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise CallFlowValidationError([CallFlowError("flow", None, None, "This flow is malformed.")])
    if len(raw_nodes) > MAX_NODES:
        raise CallFlowValidationError([
            CallFlowError("flow", None, None, f"A flow can hold at most {MAX_NODES} steps."),
        ])

    nodes: dict[str, CallFlowNode] = {}
    for rn in raw_nodes:
        nid, ntype = str(rn.get("id") or ""), str(rn.get("type") or "")
        if not nid:
            errs.append(CallFlowError("flow", None, None, "A step is missing its id."))
            continue
        if ntype not in NODE_TYPES:
            errs.append(CallFlowError("node", nid, "type", f"{ntype!r} is not a kind of step."))
            continue
        if nid in nodes:
            errs.append(CallFlowError("node", nid, None, "Two steps share the same id."))
            continue
        d = rn.get("data") or {}
        node = CallFlowNode(
            id=nid, type=ntype, name=str(d.get("name") or ntype),
            prompt=str(d.get("prompt") or ""),
            timeout_ms=int(d.get("timeout_ms") or 5000),
            max_retries=int(d.get("max_retries") or 2),
            variable=d.get("variable"),
            min_digits=int(d.get("min_digits") or 1),
            max_digits=int(d.get("max_digits") or 10),
            terminator=str(d.get("terminator") or "#"),
            destination=d.get("destination"),
            agent_id=d.get("agent_id"),
        )
        nodes[nid] = node
        errs.extend(_node_errors(rn, node))

    for re_ in raw_edges:
        src, tgt = str(re_.get("source") or ""), str(re_.get("target") or "")
        eid = str(re_.get("id") or f"{src}->{tgt}")
        if src not in nodes:
            errs.append(CallFlowError("edge", eid, "source",
                                      "This connection starts from a step that no longer exists."))
            continue
        nodes[src].out_edges.append(
            CallFlowEdge(id=eid, source=src, target=tgt, key=(re_.get("data") or {}).get("key")),
        )

    starts = [n for n in nodes.values() if n.type == "start"]
    if len(starts) != 1:
        errs.append(CallFlowError(
            "flow", None, None,
            "A flow needs exactly one start step." if not starts else "A flow can only have one start step.",
        ))

    errs.extend(_wiring_errors(nodes))
    if errs:
        raise CallFlowValidationError(errs)
    return CallFlowGraph(nodes=nodes, start_node_id=starts[0].id)


def graph_warnings(graph: CallFlowGraph) -> list[CallFlowError]:
    """Non-blocking authoring notes."""
    warns: list[CallFlowError] = []

    reachable: set[str] = set()
    stack = [graph.start_node_id]
    while stack:
        nid = stack.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        stack.extend(e.target for e in graph.nodes[nid].out_edges if e.target in graph.nodes)
    for nid, node in graph.nodes.items():
        if nid not in reachable:
            warns.append(CallFlowError("node", nid, None,
                                       "Nothing leads to this step, so the caller can never reach it."))

    for node in graph.nodes.values():
        if node.type != "menu":
            continue
        keys = {e.key for e in node.out_edges}
        if MENU_TIMEOUT not in keys:
            warns.append(CallFlowError(
                "node", node.id, None,
                "No branch for when the caller presses nothing — the menu will just repeat, "
                f"then give up after {node.max_retries} tries.",
            ))
        if MENU_INVALID not in keys:
            warns.append(CallFlowError(
                "node", node.id, None,
                "No branch for an unrecognised keypress.",
            ))

    if not any(n.is_terminal for n in graph.nodes.values()):
        warns.append(CallFlowError("flow", None, None,
                                   "No step ends the call, transfers it, or hands it to an agent."))
    return warns


def starter_graph() -> dict[str, Any]:
    """What a brand-new flow starts as: answer, greet, hang up. Valid on
    creation so a flow is publishable before it is authored."""
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "call starts", "prompt": ""},
             "position": {"x": 0, "y": 0}},
            {"id": "greeting", "type": "play",
             "data": {"name": "greeting", "prompt": "Thanks for calling."},
             "position": {"x": 0, "y": 140}},
            {"id": "done", "type": "hangup", "data": {"name": "end call", "prompt": ""},
             "position": {"x": 0, "y": 280}},
        ],
        "edges": [
            {"id": "e-start-greeting", "source": "start", "target": "greeting"},
            {"id": "e-greeting-done", "source": "greeting", "target": "done"},
        ],
    }
