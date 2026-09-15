"""tests/test_no_bare_pool_calls.py — AST tripwire for bare pool calls (T55).

The 116 pool-level `pool.fetch/fetchrow/fetchval/execute/executemany` sites
that bypassed `acquire()` entirely (design "Conversion inventory") are the
failure mode a per-module checklist demonstrably misses twice over (lesson
12/29): after cutover any such call runs on an arbitrary pooled connection
with no GUC set, and looks — from the app's side — exactly like "this
tenant has no data" rather than a bug. This walks every module under
`services/` (excluding `*/tests/*`) and fails on any `.fetch/.fetchrow/
.fetchval/.execute/.executemany` attribute call whose receiver resolves to
a name bound either to a parameter annotated `asyncpg.Pool` or to the
result of a `get_pool()`/`create_pool()` call — the two shapes every
pool-level call site in this codebase actually takes.

Deliberately does NOT flag `.acquire()` itself, nor calls on a name bound by
`async with tenant_conn(pool) as conn: ...` — `conn` there is a Connection,
not a Pool, which is exactly the shape conversion is supposed to leave
behind.
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
    """Names bound, anywhere in `node`'s subtree, either as a
    `: asyncpg.Pool`-annotated parameter or as the target of an assignment
    from `get_pool()`/`create_pool()` (bare or dotted, e.g. `db.get_pool()`,
    `self._pool = await create_pool(...)`)."""
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
    """Returns (lineno, receiver_name, method) for every bare pool-level
    call found in `source`. Exposed as a function (not just a test) so the
    "trips on a deliberately reintroduced call" case can exercise it
    directly against a scratch string, per lesson 12."""
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
    # Proves the check can actually fail (lesson 12): a scratch module with
    # the exact two shapes real call sites take before conversion — a
    # Pool-annotated parameter and a get_pool()-derived local — both trip it.
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
    # The shape conversion is supposed to LEAVE BEHIND: conn is a
    # Connection yielded by tenant_conn(pool), not the pool itself, so
    # conn.fetchrow(...) must not be flagged.
    scratch = """
from libs.tenancy import tenant_conn

async def fine(pool):
    async with tenant_conn(pool) as conn:
        return await conn.fetchrow("SELECT 1")
"""
    assert bare_pool_calls_in_source(scratch) == []
