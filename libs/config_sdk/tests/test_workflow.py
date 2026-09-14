"""Graph model + validation — pure functions, one case per runtime-breaking rule."""

from __future__ import annotations

import pytest

from libs.config_sdk.workflow import (
    WorkflowInvalid,
    graph_warnings,
    parse_graph,
    render,
    starter_graph,
)


def _node(id_, type_, name, **data):
    return {"id": id_, "type": type_, "data": {"name": name, **data}}


def _edge(id_, source, target, label, condition="something happened", **data):
    return {
        "id": id_, "source": source, "target": target,
        "data": {"label": label, "condition": condition, **data},
    }


def _graph(nodes=None, edges=None):
    return {
        "version": 1,
        "nodes": nodes if nodes is not None else [
            _node("n1", "start", "greeting", prompt="Say hello."),
            _node("n2", "agent", "booking", prompt="Book it.", tools=["book_appointment"]),
            _node("n3", "end", "goodbye", prompt="Close.", disposition="qualified"),
        ],
        "edges": edges if edges is not None else [
            _edge("e1", "n1", "n2", "caller wants to book",
                  transition_speech="Let me pull up the calendar."),
            _edge("e2", "n2", "n3", "booked"),
        ],
    }


def _errors(graph):
    with pytest.raises(WorkflowInvalid) as exc:
        parse_graph(graph)
    return exc.value.errors


def test_parses_a_valid_graph_into_nodes_and_out_edges():
    graph = parse_graph(_graph())
    assert graph.start.name == "greeting"
    assert [e.target for e in graph.start.out_edges] == ["n2"]
    assert graph.nodes["n2"].tools == ["book_appointment"]
    assert graph.nodes["n3"].disposition == "qualified"


def test_edge_label_becomes_a_callable_tool_name():
    graph = parse_graph(_graph())
    assert graph.start.out_edges[0].tool_name == "caller_wants_to_book"


def test_two_labels_collapsing_to_the_same_tool_name_is_rejected():
    # "yes" / "Yes!" both become `yes` — second would silently shadow the first.
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "yes"),
        _edge("e2", "n1", "n3", "Yes!"),
        _edge("e3", "n2", "n3", "booked"),
    ]))
    assert any("same move to the agent" in e.message for e in errors)


def test_a_non_terminal_node_with_no_outgoing_edge_is_rejected():
    errors = _errors(_graph(edges=[_edge("e1", "n1", "n3", "done")]))
    assert any(e.id == "n2" and "no way out" in e.message for e in errors)


def test_dangling_edge_target_is_rejected():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "nope", "booked"),
    ]))
    assert any(e.field == "target" for e in errors)


def test_terminal_nodes_may_not_have_outgoing_edges():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "n3", "booked"),
        _edge("e3", "n3", "n2", "again"),
    ]))
    assert any(e.id == "n3" and "nothing can lead out of it" in e.message for e in errors)


def test_start_node_may_not_have_incoming_edges():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "n1", "back"),
        _edge("e3", "n2", "n3", "booked"),
    ]))
    assert any(e.id == "n1" and "lead back into the starting point" in e.message for e in errors)


def test_exactly_one_start_and_at_least_one_end():
    assert any("exactly one starting point" in e.message for e in _errors(_graph(nodes=[
        _node("n1", "start", "a"), _node("n2", "start", "b"), _node("n3", "end", "c"),
    ], edges=[])))
    assert any("ends the call" in e.message for e in _errors(_graph(nodes=[
        _node("n1", "start", "a"), _node("n2", "agent", "b"),
    ], edges=[_edge("e1", "n1", "n2", "go")])))


def test_duplicate_node_names_are_rejected():
    # Names appear in logs/transcripts — duplicates make analytics lie.
    errors = _errors(_graph(nodes=[
        _node("n1", "start", "same"), _node("n2", "agent", "same"), _node("n3", "end", "done"),
    ]))
    assert any(e.field == "name" for e in errors)


def test_an_edge_with_no_condition_is_rejected():
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "go", condition=""),
        _edge("e2", "n2", "n3", "booked"),
    ]))
    assert any(e.field == "condition" for e in errors)


def test_a_brand_new_connection_reports_one_problem_not_two():
    # Missing both name and condition at draw time — one message, not two.
    errors = _errors(_graph(edges=[
        _edge("e1", "n1", "n2", "", condition=""),
        _edge("e2", "n2", "n3", "booked"),
    ]))
    edge_errors = [e for e in errors if e.id == "e1"]
    assert len(edge_errors) == 1
    assert "isn't set up yet" in edge_errors[0].message


def test_error_messages_are_written_for_the_person_drawing_the_flow():
    # Rendered verbatim in the editor — must not leak implementation jargon.
    jargon = ("node", "edge", "LLM", "tool name", "outgoing")
    errors = _errors(_graph(edges=[_edge("e1", "n1", "n3", "done")]))
    for e in errors:
        assert not any(word in e.message for word in jargon), e.message


def test_cycles_are_allowed():
    # Q&A loops are valid; runaway calls are bounded by max_call_duration_s.
    graph = parse_graph(_graph(edges=[
        _edge("e1", "n1", "n2", "go"),
        _edge("e2", "n2", "n3", "booked"),
        _edge("e3", "n2", "n2", "another question"),
    ]))
    assert "n2" in graph.reachable()


def test_unreachable_node_is_a_warning_not_an_error():
    graph = parse_graph({
        "nodes": [
            _node("n1", "start", "greeting"),
            _node("n2", "agent", "orphan", prompt="never reached"),
            _node("n3", "end", "goodbye"),
        ],
        "edges": [_edge("e1", "n1", "n3", "done"), _edge("e2", "n2", "n3", "done too")],
    })
    warnings = graph_warnings(graph)
    assert [w.id for w in warnings] == ["n2"]


def test_undeclared_template_variable_is_a_warning():
    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting", prompt="Hello {{ custmer_name }}"),
        _node("n2", "agent", "booking", prompt="ok"),
        _node("n3", "end", "goodbye"),
    ]))
    assert any("custmer_name" in w.message for w in graph_warnings(graph))


def test_declared_and_call_context_variables_do_not_warn():
    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting", prompt="Hi {{ caller_number }}",
              extraction={"enabled": True, "variables": [{"name": "policy_number"}]}),
        _node("n2", "agent", "booking", prompt="Your policy {{ policy_number }}"),
        _node("n3", "end", "goodbye"),
    ]))
    assert graph_warnings(graph) == []


def test_variable_declared_only_on_a_later_step_warns_at_earlier_use():
    # Extraction runs when leaving a step — a later declaration cannot fill
    # an earlier prompt, even though declared_variables() would include it.
    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting", prompt="Hello {{ policy_number }}"),
        _node("n2", "agent", "booking", prompt="ok",
              extraction={"enabled": True, "variables": [{"name": "policy_number"}]}),
        _node("n3", "end", "goodbye"),
    ]))
    warnings = graph_warnings(graph)
    assert any(
        w.id == "n1" and "policy_number" in w.message and "before every path" in w.message
        for w in warnings
    )


def test_variable_must_be_captured_on_every_path_not_just_one():
    # start → lookup → use  OR  start → use (skip). lookup captures account_id;
    # the skip branch never does, so use_account must still warn.
    graph = parse_graph({
        "nodes": [
            _node("start", "start", "greeting"),
            _node("lookup", "agent", "lookup",
                  extraction={"enabled": True, "variables": [{"name": "account_id"}]}),
            _node("use", "agent", "use_account", prompt="Account {{ account_id }}"),
            _node("end", "end", "bye"),
        ],
        "edges": [
            _edge("e1", "start", "lookup", "need lookup"),
            _edge("e2", "lookup", "use", "got it"),
            _edge("e3", "start", "use", "skip lookup"),
            _edge("e4", "use", "end", "done"),
        ],
    })
    warnings = graph_warnings(graph)
    assert any(
        w.id == "use" and "account_id" in w.message and "before every path" in w.message
        for w in warnings
    )


def test_transition_speech_may_use_vars_extracted_on_the_same_step():
    graph = parse_graph(_graph(
        nodes=[
            _node("n1", "start", "greeting",
                  extraction={"enabled": True, "variables": [{"name": "policy_number"}]}),
            _node("n2", "agent", "booking", prompt="ok"),
            _node("n3", "end", "goodbye"),
        ],
        edges=[
            _edge("e1", "n1", "n2", "go",
                  transition_speech="Got policy {{ policy_number }}"),
            _edge("e2", "n2", "n3", "booked"),
        ],
    ))
    assert graph_warnings(graph) == []


def test_transfer_node_requires_a_destination():
    errors = _errors({
        "nodes": [
            _node("n1", "start", "greeting"),
            _node("n2", "transfer", "to_human", prompt="Connecting you now."),
        ],
        "edges": [_edge("e1", "n1", "n2", "needs a person")],
    })
    assert any(e.id == "n2" and e.field == "transfer_destination" for e in errors)


def test_transfer_node_with_destination_is_valid():
    graph = parse_graph({
        "nodes": [
            _node("n1", "start", "greeting"),
            _node("n2", "transfer", "to_human", prompt="Connecting you now.",
                  transfer_destination="+15551212"),
            _node("n3", "end", "goodbye"),
        ],
        "edges": [
            _edge("e1", "n1", "n2", "needs a person"),
            _edge("e2", "n1", "n3", "all done"),
        ],
    })
    assert graph.nodes["n2"].transfer_destination == "+15551212"


def test_render_substitutes_falls_back_and_never_leaks_braces():
    # Unrendered {{ x }} reaching TTS ends up in the call recording.
    assert render("Hi {{ name }}", {"name": "Ada"}) == "Hi Ada"
    assert render("Hi {{ name | there }}", {}) == "Hi there"
    assert render("Hi {{ name }}", {}) == "Hi "
    assert render("Hi {{123}}", {}) == "Hi "
    assert render("Hi {{ }}", {}) == "Hi "
    # Unbalanced closing braces are typos — strip, don't speak them.
    assert render("Hi {{ name }", {}) == "Hi "
    assert render("Hi {{ name", {}) == "Hi "
    # Valid fallback may contain `{` without false-positive stripping.
    assert render("Hi {{ name | a{b }}", {"name": "Ada"}) == "Hi Ada"
    assert render("Hi {{ name | a{b }}", {}) == "Hi a{b"


def test_malformed_tools_and_delay_do_not_crash_validation():
    # Hand-edited JSON can send a string tools field or non-int delay.
    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting", tools="book_appointment", delayed_start_ms="nope"),
        _node("n2", "agent", "booking", prompt="ok"),
        _node("n3", "end", "goodbye"),
    ]))
    assert graph.start.tools == []
    assert graph.start.delayed_start_ms == 0


def test_malformed_data_greeting_and_speech_do_not_crash():
    # Non-object data must not AttributeError — lands as a clean validation error.
    errors = _errors({
        "nodes": [
            {"id": "n1", "type": "start", "data": "oops"},
            {"id": "n2", "type": "end", "data": {"name": "goodbye"}},
        ],
        "edges": [],
    })
    assert any(e.field == "name" for e in errors)

    graph = parse_graph(_graph(
        nodes=[
            _node("n1", "start", "greeting", greeting=123),
            _node("n2", "agent", "booking", prompt="ok"),
            _node("n3", "end", "goodbye"),
        ],
        edges=[
            _edge("e1", "n1", "n2", "go", transition_speech=99),
            _edge("e2", "n2", "n3", "booked"),
        ],
    ))
    assert graph.start.greeting == "123"
    assert graph.start.out_edges[0].transition_speech == "99"


def test_null_extraction_name_is_ignored():
    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting",
              extraction={"enabled": True, "variables": [{"name": None}]}),
        _node("n2", "agent", "booking", prompt="ok"),
        _node("n3", "end", "goodbye"),
    ]))
    assert graph.declared_variables() == set()


def test_the_starter_graph_carries_the_tools_it_is_given():
    # Node.tools is default-deny — migrations must pass existing tools through.
    graph = parse_graph(starter_graph("hi", "be nice", ["book_appointment"]))
    assert graph.start.tools == ["book_appointment"]
    assert parse_graph(starter_graph("hi", "be nice")).start.tools == []


def test_the_starter_graph_carries_knowledge_base_ids():
    graph = parse_graph(starter_graph("hi", "be nice", knowledge_base_ids=["kb1"]))
    assert graph.start.knowledge_base_ids == ["kb1"]
    assert parse_graph(starter_graph("hi", "be nice")).start.knowledge_base_ids == []


def test_delayed_start_ms_is_capped():
    from libs.config_sdk.workflow import _MAX_DELAYED_START_MS

    graph = parse_graph(_graph(nodes=[
        _node("n1", "start", "greeting", delayed_start_ms=30_000),
        _node("n2", "agent", "booking", prompt="ok"),
        _node("n3", "end", "goodbye"),
    ]))
    assert graph.start.delayed_start_ms == _MAX_DELAYED_START_MS


def test_graphs_equivalent_ignores_node_positions():
    from libs.config_sdk.workflow import graphs_equivalent

    a = starter_graph("hi", "be nice")
    b = {
        **a,
        "nodes": [{**n, "position": {"x": 1, "y": 2}} for n in a["nodes"]],
    }
    assert graphs_equivalent(a, b)
    b["nodes"][1]["data"] = {**b["nodes"][1]["data"], "greeting": "bye"}
    assert not graphs_equivalent(a, b)


def test_graphs_equivalent_ignores_node_and_edge_order():
    from libs.config_sdk.workflow import graphs_equivalent

    a = starter_graph("hi", "be nice")
    b = {
        **a,
        "nodes": list(reversed(a["nodes"])),
        "edges": list(reversed(a["edges"])),
    }
    assert graphs_equivalent(a, b)


def test_graphs_equivalent_ignores_react_flow_chrome():
    from libs.config_sdk.workflow import graphs_equivalent

    a = starter_graph("hi", "be nice")
    b = {
        **a,
        "nodes": [
            {**n, "selected": True, "dragging": False, "width": 200, "height": 80,
             "measured": {"width": 200, "height": 80}}
            for n in a["nodes"]
        ],
        "edges": [{**e, "selected": True} for e in a["edges"]],
    }
    assert graphs_equivalent(a, b)
