"""AST tripwire: no `services/` code queries a Pool directly.

A bare pool call runs with no tenant GUC, so under RLS it silently sees no rows.
Use `tenant_conn(pool)` instead.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOTS = [REPO_ROOT / "services"]

_POOL_METHODS = {"fetch", "fetchrow", "fetchval", "execute", "executemany"}
_POOL_FACTORY_NAMES = {"get_pool", "create_pool"}


def _is_pool_annotation(annotation: ast.AST | None) -> bool:
    return annotation is not None and "Pool" in ast.dump(annotation)


def _is_pool_factory_call(call: ast.AST) -> bool:
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _POOL_FACTORY_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in _POOL_FACTORY_NAMES
    return False


def _bound_pool_names(node: ast.AST) -> set[str]:
    """Names bound as a Pool-annotated parameter or from get_pool()/create_pool()."""
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.arg) and _is_pool_annotation(n.annotation):
            names.add(n.arg)
        if isinstance(n, ast.Assign) and _is_pool_factory_call(n.value):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
                elif isinstance(tgt, ast.Attribute):
                    names.add(tgt.attr)
    return names


def bare_pool_calls_in_source(source: str) -> list[tuple[int, str, str]]:
    """(lineno, receiver_name, method) for every bare pool-level call in `source`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[tuple[int, str, str]] = []
    module_pool_names = _bound_pool_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local_names = module_pool_names | _bound_pool_names(node)
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr not in _POOL_METHODS:
                continue
            receiver = call.func.value
            name = (
                receiver.id if isinstance(receiver, ast.Name)
                else receiver.attr if isinstance(receiver, ast.Attribute)
                else None
            )
            if name in local_names:
                findings.append((call.lineno, name, call.func.attr))
    return findings


def _production_modules() -> list[pathlib.Path]:
    modules = []
    for root in SEARCH_ROOTS:
        for path in root.rglob("*.py"):
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            modules.append(path)
    return modules


def test_zero_bare_pool_calls_across_services():
    failures = []
    for path in _production_modules():
        findings = bare_pool_calls_in_source(path.read_text())
        for lineno, name, method in findings:
            failures.append((str(path.relative_to(REPO_ROOT)), lineno, name, method))
    assert failures == [], (
        f"bare pool-level calls found (path, line, receiver, method): {failures}"
    )


def test_walker_trips_on_a_deliberately_reintroduced_bare_pool_call():
    scratch = """
import asyncpg

async def get_pool() -> asyncpg.Pool: ...

async def leaky(pool: asyncpg.Pool):
    return await pool.fetchrow("SELECT 1")

async def also_leaky():
    pool = await get_pool()
    return await pool.fetch("SELECT 1")
"""
    findings = bare_pool_calls_in_source(scratch)
    assert len(findings) == 2
    assert {f[2] for f in findings} == {"fetchrow", "fetch"}


def test_walker_does_not_flag_a_connection_acquired_via_tenant_conn():
    scratch = """
from libs.tenancy import tenant_conn

async def fine(pool):
    async with tenant_conn(pool) as conn:
        return await conn.fetchrow("SELECT 1")
"""
    assert bare_pool_calls_in_source(scratch) == []
