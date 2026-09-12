"""
services/toolexec/graph.py — chain ordering and upstream-value extraction.

resolve_order() is a pure function over an in-memory dependency tree (no
DB, no network): the executor is responsible for assembling that tree from
custom_api_params rows (the edges ARE the 'upstream' params — see
database/schema.sql's custom_api_params comment) before calling this
module, and for re-deriving it fresh on every call rather than trusting
the denormalized custom_apis.chain_levels (AC 11 backstop — chain_levels
can drift, this cannot).

Each node is a plain dict: {"id": ..., "name": ..., "upstream_apis": [...]}
where "upstream_apis" holds this api's own declared upstream dependencies,
each shaped the same way, recursively. A node with no "upstream_apis" key
(or an empty list) is a leaf.

Chain steps run strictly sequentially in v1 — resolve_order returns a flat
post-order list, never batched by level. See the design's Risks section
for why: no tenant topology exists yet to show fan-out is common, and
in-flight concurrency lands on the partial-failure/idempotency bookkeeping
where a bug means a duplicated side effect. Do not add wave/parallel
execution here.
"""

from __future__ import annotations

import re
from typing import Any

MAX_CHAIN_LEVELS = 4


def resolve_order(api: dict, max_levels: int) -> list[dict]:
    """Post-order DFS over api['upstream_apis'], deduplicated by id (a
    diamond's shared upstream runs, and appears, once), with the target
    itself always last.

    Raises ValueError('depth_limit_exceeded') if any path from the target
    down through its upstream dependencies exceeds max_levels, or
    ValueError('cycle_detected') if a dependency edge closes a cycle.
    Neither check runs after the fact — both stop the walk on the spot, so
    no HTTP call is ever considered for a chain this refuses.

    The dedupe is memoized (a node already fully resolved is not re-walked
    or re-appended), but a memoized node's own subtree height is cached
    alongside it and re-checked against ancestor depth on every path that
    reaches the node again — not only the first. Without that, a node
    resolved cheaply via a shallow branch would silently absolve a deeper
    branch that reaches the same node, letting that deeper branch's own
    (unexamined) subtree push the true longest path past max_levels.
    """
    order: list[dict] = []
    resolved_ids: set[str] = set()
    # node_id -> height of its own subtree, counting the node itself down
    # to its deepest already-resolved leaf. Computed once, on first
    # resolution, and reused (never re-walked) on every later path.
    subtree_height: dict[str, int] = {}

    # The ceiling lives here, not just in a caller's comment: max_levels can
    # only ever be LOWERED by a caller (a per-agent override), never raised
    # past the platform maximum — a caller that forgets to clamp before
    # calling still gets the real ceiling enforced.
    max_levels = min(max_levels, MAX_CHAIN_LEVELS)

    def _walk(node: dict, ancestor_ids: frozenset[str]) -> None:
        node_id = node["id"]
        if node_id in ancestor_ids:
            raise ValueError("cycle_detected")
        depth = len(ancestor_ids) + 1

        if node_id in resolved_ids:
            # Diamond: don't re-walk or re-append it, but THIS path's own
            # depth to it still has to afford its already-known height —
            # the first path to resolve it may have been shallower.
            if depth + subtree_height[node_id] - 1 > max_levels:
                raise ValueError("depth_limit_exceeded")
            return

        if depth > max_levels:
            raise ValueError("depth_limit_exceeded")

        next_ancestor_ids = ancestor_ids | {node_id}
        height = 1
        for upstream in node.get("upstream_apis") or []:
            _walk(upstream, next_ancestor_ids)
            height = max(height, 1 + subtree_height[upstream["id"]])

        subtree_height[node_id] = height
        resolved_ids.add(node_id)
        order.append(node)

    _walk(api, frozenset())
    return order


# Sentinel returned by extract() on a miss — never an exception, since a
# missing upstream value is an ordinary, expected outcome (the step that
# needed it is recorded 'failed'/'upstream_value_missing' by the caller,
# not crashed).
class _Missing:
    def __repr__(self) -> str:
        return "<MISSING>"


MISSING = _Missing()

_SEGMENT_RE = re.compile(r"(?P<name>[^\[\].]+)?(?P<indices>(?:\[\d+\])*)")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def extract(response: Any, json_path: str) -> Any:
    """A small JSONPath subset sufficient for custom_apis.sensitive_response_paths
    / custom_api_params.upstream_json_path: '$.a.b', '$.items[0].id'. Returns
    MISSING — never raises — if any segment is absent, the wrong shape
    (indexing into a non-list, keying into a non-dict), or out of range.
    """
    if not json_path.startswith("$"):
        return MISSING
    path = json_path[1:].removeprefix(".")
    if path == "":
        return response

    current = response
    for raw_segment in path.split("."):
        if raw_segment == "":
            return MISSING
        match = _SEGMENT_RE.fullmatch(raw_segment)
        if match is None:
            return MISSING

        name = match.group("name")
        if name is not None:
            if not isinstance(current, dict) or name not in current:
                return MISSING
            current = current[name]

        for index_str in _INDEX_RE.findall(match.group("indices") or ""):
            index = int(index_str)
            if not isinstance(current, list) or index >= len(current):
                return MISSING
            current = current[index]

    return current
