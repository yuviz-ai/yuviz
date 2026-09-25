"""R2-1: an AST walk over every module under services/telephony/ that fails
on any Call to assert_tenant_access/resolve_caller_tenant/
resolve_outbound_identity not wrapped in an Await (and on any `def` —
rather than `async def` — definition of the latter two). The walk asserts
its own found-call count is non-zero so it cannot pass vacuously."""

from __future__ import annotations

import ast
from pathlib import Path

_TARGET_NAMES = {"assert_tenant_access", "resolve_caller_tenant", "resolve_outbound_identity"}
_ASYNC_DEF_TARGETS = {"resolve_caller_tenant", "resolve_outbound_identity"}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_every_guard_call_is_awaited_and_definitions_are_async():
    package_dir = Path(__file__).resolve().parent.parent
    found_calls = 0
    unawaited: list[str] = []
    non_async_defs: list[str] = []

    for py_file in package_dir.glob("*.py"):
        source = py_file.read_text()
        tree = ast.parse(source, filename=str(py_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in _TARGET_NAMES:
                    found_calls += 1
            if isinstance(node, ast.FunctionDef) and node.name in _ASYNC_DEF_TARGETS:
                non_async_defs.append(f"{py_file.name}:{node.name} (def, not async def)")
            if isinstance(node, ast.AsyncFunctionDef):
                continue

        # Every Call node reachable directly under an Await is "awaited";
        # anything else with a matching name is a bare, un-awaited call.
        awaited_call_ids = {
            id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in _TARGET_NAMES:
                if id(node) not in awaited_call_ids:
                    unawaited.append(f"{py_file.name}: bare call to {_call_name(node)}")

    assert found_calls > 0, "the walk found zero guard calls — it cannot be exercising anything"
    assert unawaited == []
    assert non_async_defs == []
