"""Dry-run tests for CallFlowRunner — no providers, no audio, no I/O."""

from __future__ import annotations

from libs.config_sdk.callflow import CallFlowEdge, CallFlowGraph, CallFlowNode, parse_graph
from libs.config_sdk.models import CallFlow

from ..callflow.runner import (
    Dial, Handoff, Hangup, Listen, Speak, SetVoice, Store, graph_for_flow,
)
from ..callflow.runner import CallFlowRunner


def _menu_graph(**menu_extra):
    data = {"name": "menu", "prompt": "Press 1 for sales.", "timeout_ms": 5000, "max_retries": 2}
    data.update(menu_extra)
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "menu", "type": "menu", "data": data},
            {"id": "sales", "type": "hangup", "data": {"name": "sales", "prompt": "Sales."}},
            {"id": "bye", "type": "hangup", "data": {"name": "bye", "prompt": "Bye."}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "menu"},
            {"id": "e1", "source": "menu", "target": "sales", "data": {"key": "1"}},
        ],
    }


def _menu_with_timeout_fallback_graph():
    # Timeout branch only: unmatched digits still replay.
    g = _menu_graph()
    g["nodes"].append({"id": "gone", "type": "hangup", "data": {"name": "gone", "prompt": "Gone."}})
    g["edges"].append({"id": "e2", "source": "menu", "target": "gone", "data": {"key": "timeout"}})
    return g


def _collect_graph(**collect_extra):
    data = {
        "name": "collect", "prompt": "Enter your PIN.", "timeout_ms": 5000, "max_retries": 1,
        "variable": "pin", "min_digits": 4, "max_digits": 4, "terminator": "#",
    }
    data.update(collect_extra)
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "collect", "type": "collect", "data": data},
            {"id": "next", "type": "hangup", "data": {"name": "next", "prompt": "Thanks."}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "collect"},
            {"id": "e1", "source": "collect", "target": "next"},
        ],
    }


def _runner(raw_graph, *, tts_config_id=None, variables=None) -> CallFlowRunner:
    return CallFlowRunner(parse_graph(raw_graph), tts_config_id=tts_config_id, variables=variables)


# ── 1. menu ──────────────────────────────────────────────────────────────

def test_menu_matching_digit_branches_immediately():
    runner = _runner(_menu_graph())
    runner.open()
    actions = runner.on_digit("1")
    assert actions[-1] == Hangup("flow_complete")
    assert runner.node.id == "sales"


def test_menu_explicit_timeout_edge_taken_with_retries_untouched():
    runner = _runner(_menu_with_timeout_fallback_graph())
    runner.open()
    runner.on_digit("9")  # unmatched, no invalid branch -> replay
    actions = runner.on_timeout()
    assert actions[-1] == Hangup("flow_complete")
    assert runner.node.id == "gone"


def test_menu_unbranched_timeout_replays_then_exhausts():
    runner = _runner(_menu_graph())  # no explicit timeout/invalid edges
    runner.open()
    first = runner.on_timeout()
    assert first[0] == Speak("Press 1 for sales.")
    assert isinstance(first[1], Listen)
    assert runner.node.id == "menu"  # replayed, did not advance

    second = runner.on_timeout()  # max_retries=2 -> this is the 2nd
    assert isinstance(second[-1], Listen)

    third = runner.on_timeout()  # negative control: one shorter (above) still Listens
    assert third == [Hangup("retries_exhausted")]


def test_menu_unmatched_digit_replays_via_invalid_fallback():
    runner = _runner(_menu_graph())
    runner.open()
    actions = runner.on_digit("5")
    assert actions[0] == Speak("Press 1 for sales.")
    assert runner.node.id == "menu"


# ── 2. collect ───────────────────────────────────────────────────────────

def test_collect_terminator_at_min_digits_stores_and_excludes_terminator():
    runner = _runner(_collect_graph())
    runner.open()
    for d in "123":
        assert runner.on_digit(d) == []
    actions = runner.on_digit("4")
    assert Store("pin", "1234", False) in actions
    assert runner.node.id == "next"
    assert runner.variables["pin"] == "1234"


def test_collect_max_digits_auto_submits_without_terminator():
    runner = _runner(_collect_graph(max_digits=4, terminator="#"))
    runner.open()
    runner.on_digit("1"); runner.on_digit("2"); runner.on_digit("3")
    actions = runner.on_digit("4")  # 4th digit == max_digits, no "#" pressed
    assert Store("pin", "1234", False) in actions


def test_collect_terminator_submits_below_max_digits():
    # Buffer below max_digits, so only the terminator can cause submission.
    runner = _runner(_collect_graph(min_digits=2, max_digits=4, terminator="#"))
    runner.open()
    assert runner.on_digit("1") == []
    actions = runner.on_digit("2")
    assert actions == []  # still short of both min_digits-then-terminator and max_digits
    submit_actions = runner.on_digit("#")
    assert Store("pin", "12", False) in submit_actions
    assert runner.node.id == "next"
    assert runner.variables["pin"] == "12"


def test_collect_terminator_below_min_digits_replays_then_exhausts():
    runner = _runner(_collect_graph(min_digits=4, max_digits=6, max_retries=1))
    runner.open()
    runner.on_digit("1"); runner.on_digit("2")
    first = runner.on_digit("#")  # only 2 digits, min is 4 -> invalid attempt
    assert first[0] == Speak("Enter your PIN.")
    assert runner.node.id == "collect"

    runner.on_digit("1"); runner.on_digit("2")
    second = runner.on_digit("#")  # 2nd invalid attempt, max_retries=1
    assert second == [Hangup("retries_exhausted")]


def test_collect_timeout_below_min_digits_replays_then_exhausts():
    runner = _runner(_collect_graph(min_digits=4, max_retries=1))
    runner.open()
    runner.on_digit("1")
    first = runner.on_timeout()
    assert isinstance(first[-1], Listen)
    second = runner.on_timeout()
    assert second == [Hangup("retries_exhausted")]


def _collect_only_graph(**node_kwargs) -> CallFlowGraph:
    """Build a graph directly, bypassing parse_graph() (which coerces an empty terminator to "#")."""
    collect = CallFlowNode(
        id="collect", type="collect", name="collect", prompt="Enter your PIN.",
        timeout_ms=5000, max_retries=1, variable="pin",
        out_edges=[CallFlowEdge(id="e1", source="collect", target="next")],
        **node_kwargs,
    )
    start = CallFlowNode(
        id="start", type="start", name="start",
        out_edges=[CallFlowEdge(id="e0", source="start", target="collect")],
    )
    next_ = CallFlowNode(id="next", type="hangup", name="next", prompt="Thanks.")
    return CallFlowGraph(nodes={"start": start, "collect": collect, "next": next_}, start_node_id="start")


def test_collect_empty_terminator_submits_only_on_max_digits_or_timeout():
    graph = _collect_only_graph(terminator="", min_digits=2, max_digits=4)
    runner = CallFlowRunner(graph, tts_config_id=None)
    runner.open()
    # With no terminator, "#" is just a buffered digit.
    assert runner.on_digit("#") == []
    actions = runner.on_timeout()  # 1 digit buffered, below min_digits=2
    assert isinstance(actions[-1], Listen)

    graph2 = _collect_only_graph(terminator="", min_digits=1, max_digits=2)
    runner2 = CallFlowRunner(graph2, tts_config_id=None)
    runner2.open()
    runner2.on_digit("1")
    actions2 = runner2.on_digit("2")  # hits max_digits
    assert Store("pin", "12", False) in actions2


# ── 3. variable seeding / non-DTMF input ────────────────────────────────

def test_collect_overwrites_seeded_variable_last_write_wins():
    runner = _runner(_collect_graph(), variables={"pin": "0000"})
    runner.open()
    for d in "9999":
        runner.on_digit(d)
    assert runner.variables["pin"] == "9999"


def test_non_dtmf_digit_ignored_menu():
    runner = _runner(_menu_graph())
    runner.open()
    assert runner.on_digit("x") == []
    assert runner.node.id == "menu"


# ── 3a. voice ────────────────────────────────────────────────────────────

def test_setvoice_carries_only_constructor_value_never_graph_value():
    graph = _menu_graph()
    graph["nodes"][0]["data"]["tts_config_id"] = "other-tenants-tts"
    runner = _runner(graph, tts_config_id=None)  # Config resolved to null
    actions = runner.open()
    assert actions[0] == SetVoice(None)


def test_setvoice_carries_constructor_value():
    runner = _runner(_menu_graph(), tts_config_id="validated-tts")
    actions = runner.open()
    assert actions[0] == SetVoice("validated-tts")


# ── 3b. sensitive collect ────────────────────────────────────────────────

def test_sensitive_collect_excluded_from_variables_present_in_flow_variables():
    graph = _collect_graph(sensitive=True)
    graph["nodes"][1]["data"]["prompt"] = "Confirm your PIN, {{ pin | none }}."
    runner = _runner(graph)
    runner.open()
    for d in "1234":
        runner.on_digit(d)
    assert "pin" not in runner.variables
    assert runner.flow_variables["pin"] == "1234"


def test_non_sensitive_collect_negative_control_present_in_both():
    graph = _collect_graph(sensitive=False)
    runner = _runner(graph)
    runner.open()
    for d in "1234":
        runner.on_digit(d)
    assert runner.variables["pin"] == "1234"
    assert runner.flow_variables["pin"] == "1234"


# ── 4. terminal nodes / budget ───────────────────────────────────────────

def test_dial_emits_speak_then_dial():
    graph = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "d", "type": "dial", "data": {"name": "d", "prompt": "Connecting.", "destination": "+15551234"}},
        ],
        "edges": [{"id": "e0", "source": "start", "target": "d"}],
    }
    runner = _runner(graph)
    actions = runner.open()
    assert actions[-2:] == [Speak("Connecting."), Dial("+15551234")]


def test_hangup_emits_speak_then_hangup():
    graph = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "h", "type": "hangup", "data": {"name": "h", "prompt": "Bye now."}},
        ],
        "edges": [{"id": "e0", "source": "start", "target": "h"}],
    }
    runner = _runner(graph)
    actions = runner.open()
    assert actions[-2:] == [Speak("Bye now."), Hangup("flow_complete")]


def test_agent_node_emits_handoff():
    graph = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "a", "type": "agent", "data": {"name": "a", "agent_id": "agent-123"}},
        ],
        "edges": [{"id": "e0", "source": "start", "target": "a"}],
    }
    runner = _runner(graph)
    actions = runner.open()
    assert actions[-1] == Handoff("agent-123")


def test_transition_budget_trips_on_self_looping_play_chain():
    # A play<->play cycle walked synchronously in open(); built directly to bypass parse_graph's node cap.
    start = CallFlowNode(id="start", type="start", name="start",
                         out_edges=[CallFlowEdge(id="e0", source="start", target="p0")])
    p0 = CallFlowNode(id="p0", type="play", name="p0", prompt="hi",
                      out_edges=[CallFlowEdge(id="e1", source="p0", target="p1")])
    p1 = CallFlowNode(id="p1", type="play", name="p1", prompt="hi",
                      out_edges=[CallFlowEdge(id="e2", source="p1", target="p0")])
    graph = CallFlowGraph(nodes={"start": start, "p0": p0, "p1": p1}, start_node_id="start")
    runner = CallFlowRunner(graph, tts_config_id=None)
    actions = runner.open()
    assert actions[-1] == Hangup("flow_budget")


# ── 6/7. cache + isolation ───────────────────────────────────────────────

def test_graph_for_flow_caches_and_reparses_on_version_bump(monkeypatch):
    import services.conversation.callflow.runner as runner_mod

    calls = []
    real_parse = runner_mod.parse_graph

    def counting_parse(raw):
        calls.append(1)
        return real_parse(raw)

    monkeypatch.setattr(runner_mod, "parse_graph", counting_parse)

    flow = CallFlow(id="f1", tenant_slug="acme", config_version=1, graph=_menu_graph())
    graph_for_flow(flow)
    graph_for_flow(flow)
    assert len(calls) == 1  # two "sessions" on the same version parse once

    bumped = CallFlow(id="f1", tenant_slug="acme", config_version=2, graph=_menu_graph())
    graph_for_flow(bumped)
    assert len(calls) == 2  # bumped config_version re-parses


def test_two_runners_over_one_cached_graph_advance_independently():
    flow = CallFlow(id="f1", tenant_slug="acme", config_version=1, graph=_collect_graph())
    graph = graph_for_flow(flow)

    runner_a = CallFlowRunner(graph, tts_config_id=None)
    runner_b = CallFlowRunner(graph, tts_config_id=None)
    runner_a.open()
    runner_b.open()

    for d in "1234":
        runner_a.on_digit(d)

    assert runner_a.node.id == "next"
    assert runner_a.variables["pin"] == "1234"
    assert runner_b.node.id == "collect"  # untouched
    assert "pin" not in runner_b.variables
