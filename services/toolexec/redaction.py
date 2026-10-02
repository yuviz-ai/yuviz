"""Single redaction control for step arguments and responses before persistence or return.

Paths use graph.extract()'s JSONPath subset; a bare name means '$.<name>'; absent paths are no-ops.
"""

from __future__ import annotations

import copy
from typing import Any

from .graph import _INDEX_RE, _SEGMENT_RE

REDACTED = "[redacted]"


def _segments(path_or_key: str) -> list[str]:
    if path_or_key.startswith("$"):
        rest = path_or_key[1:].removeprefix(".")
        return rest.split(".") if rest else []
    return [path_or_key]  # bare key shorthand for "$.<key>"


def _redact_one(payload: Any, path_or_key: str) -> None:
    segments = _segments(path_or_key)
    if not segments:
        return  # "$" itself — nothing to redact into

    current = payload
    container: Any = None
    key: str | int | None = None

    for raw_segment in segments:
        match = _SEGMENT_RE.fullmatch(raw_segment)
        if match is None:
            return  # malformed path — no-op, not an exception

        name = match.group("name")
        if name is not None:
            if not isinstance(current, dict) or name not in current:
                return
            container, key = current, name
            current = current[name]

        for index_str in _INDEX_RE.findall(match.group("indices") or ""):
            index = int(index_str)
            if not isinstance(current, list) or index >= len(current):
                return
            container, key = current, index
            current = current[index]

    if container is not None:
        container[key] = REDACTED


def redact(payload: Any, paths: list[str]) -> Any:
    """Return a deep copy with each path's value replaced by REDACTED."""
    result = copy.deepcopy(payload)
    for path in paths:
        _redact_one(result, path)
    return result
