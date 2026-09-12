"""
Unit tests for services/toolexec/graph.py (T6) — no DB, no network. Nodes
are the plain-dict shape graph.resolve_order() expects:
{"id", "name", "upstream_apis": [...]}.
"""

from __future__ import annotations

import pytest

from services.toolexec import graph


def _node(id_, name, upstream_apis=None):
    return {"id": id_, "name": name, "upstream_apis": upstream_apis or []}


def test_four_level_chain_post_order():
    # level1 (leaf) <- level2 <- level3 <- level4 (target)
    level1 = _node("1", "level1")
    level2 = _node("2", "level2", [level1])
    level3 = _node("3", "level3", [level2])
    level4 = _node("4", "level4", [level3])

    order = graph.resolve_order(level4, max_levels=4)
    assert [n["id"] for n in order] == ["1", "2", "3", "4"]


def test_diamond_dedupe_shared_upstream_runs_once():
    leaf = _node("leaf", "leaf")
    a = _node("a", "a", [leaf])
    b = _node("b", "b", [leaf])
    root = _node("root", "root", [a, b])

    order = graph.resolve_order(root, max_levels=4)
    ids = [n["id"] for n in order]
    assert ids.count("leaf") == 1
    assert ids == ["leaf", "a", "b", "root"]


def test_depth_limit_exceeded_at_five_levels():
    node = _node("1", "l1")
    for i in range(2, 6):
        node = _node(str(i), f"l{i}", [node])  # now 5 levels deep

    with pytest.raises(ValueError, match="depth_limit_exceeded"):
        graph.resolve_order(node, max_levels=4)


def test_depth_limit_exceeded_at_per_agent_override_of_two():
    leaf = _node("1", "l1")
    mid = _node("2", "l2", [leaf])
    root = _node("3", "l3", [mid])  # 3 levels deep

    with pytest.raises(ValueError, match="depth_limit_exceeded"):
        graph.resolve_order(root, max_levels=2)

    # Sanity: the same 3-level chain is fine under the default ceiling.
    order = graph.resolve_order(root, max_levels=4)
    assert [n["id"] for n in order] == ["1", "2", "3"]


def test_depth_limit_enforced_through_a_shared_upstream_on_unequal_branches():
    """DEFECT REPRODUCTION (security audit finding 3, graph.py:54-55): the
    `if node_id in resolved_ids: return` dedupe fires before the walk into
    that node's own subtree, so a node first resolved on a SHALLOW branch is
    never re-examined — nor is anything beneath it — when reached again via
    a DEEPER branch.

    Construction: R -> [A -> B -> C]  and  R -> D -> E -> B
    B is shared. Reached via A at depth 3 (fully resolved there first, since
    A is visited before D in upstream_apis order), then reached again via D/E
    at depth 5. The longest path R->D->E->B->C is 5 levels, so
    resolve_order(root, max_levels=4) must raise depth_limit_exceeded — but
    the dedupe short-circuits the second, deeper visit to B before C's own
    depth (5) is ever checked, so this currently returns an order instead.

    This test is EXPECTED TO FAIL against the shipped code. Do not weaken
    the assertion to make it pass — report it as the defect it is."""
    c = _node("C", "c")
    b = _node("B", "b", [c])
    a = _node("A", "a", [b])
    e = _node("E", "e", [b])
    d = _node("D", "d", [e])
    root = _node("R", "r", [a, d])

    with pytest.raises(ValueError, match="depth_limit_exceeded"):
        graph.resolve_order(root, max_levels=4)


def test_cycle_detected():
    a: dict = {"id": "a", "name": "a", "upstream_apis": []}
    b: dict = {"id": "b", "name": "b", "upstream_apis": [a]}
    a["upstream_apis"] = [b]  # a -> b -> a

    with pytest.raises(ValueError, match="cycle_detected"):
        graph.resolve_order(a, max_levels=4)


def test_extract_hit():
    response = {"data": {"id": "order-123"}, "items": [{"id": "x"}, {"id": "y"}]}
    assert graph.extract(response, "$.data.id") == "order-123"
    assert graph.extract(response, "$.items[1].id") == "y"


def test_extract_miss_is_not_an_exception():
    response = {"data": {"id": "order-123"}}
    assert graph.extract(response, "$.data.missing_field") is graph.MISSING
    assert graph.extract(response, "$.no.such.path") is graph.MISSING
    assert graph.extract(response, "$.data.id[0]") is graph.MISSING  # indexing into a str
    assert graph.extract([], "$.items[5]") is graph.MISSING
