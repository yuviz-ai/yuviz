"""
services/toolexec/redaction.py — the AC 14 redaction control.

redact(payload, paths) is applied to step arguments (against
custom_api_params.sensitive param NAMES) and to step responses (against
custom_apis.sensitive_response_paths JSON PATHS) before persistence,
before success_template interpolation, and before a response ever leaves
this service — the same function for all three, so there is exactly one
place that decides what "[redacted]" means.

Paths use the same tiny JSONPath subset graph.extract() consumes
('$.a.b[0].c'); a bare name with no leading '$' (a sensitive param name,
which indexes a flat arguments dict) is shorthand for '$.<name>'. A path
absent from a given payload is a no-op, not an error: sensitive_response_paths
is declared once for the whole API, not guaranteed present on every
response shape.
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
    """Returns a deep copy of payload with the value at each path in
    `paths` replaced by "[redacted]" at whatever nesting depth it sits,
    leaving every sibling field and the rest of the structure untouched.
    """
    result = copy.deepcopy(payload)
    for path in paths:
        _redact_one(result, path)
    return result
