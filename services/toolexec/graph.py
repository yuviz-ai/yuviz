"""Chain ordering and upstream-value extraction over {"id", "name", "upstream_apis": [...]} trees.

Steps run strictly sequentially; don't add parallel execution (risks duplicated side effects).
"""

from __future__ import annotations

import re
from typing import Any

MAX_CHAIN_LEVELS = 4


def resolve_order(api: dict, max_levels: int) -> list[dict]:
    """Post-order DFS deduplicated by id, target last.

    Raises ValueError('depth_limit_exceeded' | 'cycle_detected'); depth is re-checked on every path to a shared node.
    """
    order: list[dict] = []
    resolved_ids: set[str] = set()
    subtree_height: dict[str, int] = {}

    # Callers can only lower the platform ceiling, never raise it.
    max_levels = min(max_levels, MAX_CHAIN_LEVELS)

    def _walk(node: dict, ancestor_ids: frozenset[str]) -> None:
        node_id = node["id"]
        if node_id in ancestor_ids:
            raise ValueError("cycle_detected")
        depth = len(ancestor_ids) + 1

        if node_id in resolved_ids:
            # Diamond: the first path to resolve it may have been shallower.
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


# Returned by extract() on a miss; a missing value is expected, not exceptional.
class _Missing:
    def __repr__(self) -> str:
        return "<MISSING>"


MISSING = _Missing()

_SEGMENT_RE = re.compile(r"(?P<name>[^\[\].]+)?(?P<indices>(?:\[\d+\])*)")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def extract(response: Any, json_path: str) -> Any:
    """Tiny JSONPath subset ('$.a.b', '$.items[0].id'); returns MISSING instead of raising."""
    value, _reason = extract_with_reason(response, json_path)
    return value


# NO_MATCH: path indexes into an empty list (search found nothing).
# PATH_ABSENT: anything else; a config bug, not an answer.
NO_MATCH = "no_match"
PATH_ABSENT = "path_absent"


def extract_with_reason(response: Any, json_path: str) -> tuple[Any, str | None]:
    """Return (value, None) on a hit, or (MISSING, NO_MATCH | PATH_ABSENT)."""
    if not json_path.startswith("$"):
        return MISSING, PATH_ABSENT
    path = json_path[1:].removeprefix(".")
    if path == "":
        return response, None

    current = response
    for raw_segment in path.split("."):
        if raw_segment == "":
            return MISSING, PATH_ABSENT
        match = _SEGMENT_RE.fullmatch(raw_segment)
        if match is None:
            return MISSING, PATH_ABSENT

        name = match.group("name")
        if name is not None:
            if not isinstance(current, dict) or name not in current:
                return MISSING, PATH_ABSENT
            current = current[name]

        for index_str in _INDEX_RE.findall(match.group("indices") or ""):
            index = int(index_str)
            if not isinstance(current, list):
                return MISSING, PATH_ABSENT
            if index >= len(current):
                return MISSING, (NO_MATCH if not current else PATH_ABSENT)
            current = current[index]

    return current, None
