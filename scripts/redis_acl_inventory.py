#!/usr/bin/env python3
"""Derive each Redis client's key patterns, commands and channels from its source (ACL input)."""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CLIENTS: dict[str, list[str]] = {
    "gateway": ["gateway/src", "gateway/include"],
    "config": ["services/config", "libs/config_sdk/test_credentials.py"],
    "did": ["services/did"],
    "knowledge": ["services/knowledge"],
    "conversation": ["services/conversation", "libs/config_sdk", "libs/knowledge_sdk"],
    "telephony": ["services/telephony", "libs/telephony_sdk/did_route.py"],
    "campaigns": ["services/campaigns"],
}

_CLIENT_METHODS = {
    "get": "get", "set": "set", "setex": "setex", "delete": "del", "exists": "exists", "mget": "mget",
    "scan_iter": "scan", "publish": "publish", "subscribe": "subscribe", "xadd": "xadd",
}
_CACHE_METHODS = {"get_json": "get", "set_json": "set", "invalidate": "del", "publish": "publish"}
_CACHE_RECEIVERS = {"cache", "config_cache"}
_KEY_BUILDERS = {"slug_key", "_key"}
_EXTRA_COMMANDS = {"subscribe": ["unsubscribe"]}
_SECRET_SCHEMES = {"env:", "k8s:", "enc:"}
_KEY_LITERAL = re.compile(r"^([a-z][a-z0-9_]*):(?!//)(\S*)$")
_CPP_COMMAND = re.compile(r'redisCommand\([^,]+,\s*"([A-Za-z]+)')
_CPP_KEY = re.compile(r'"([a-z][a-z0-9_]*:\S*?)"')


def _py_files(paths: list[Path]):
    for p in paths:
        found = [p] if p.is_file() else sorted(p.rglob("*.py"))
        yield from (f for f in found if "tests" not in f.parts and not f.name.startswith("test_") or f.name == "test_credentials.py")


def _key_pattern(literal: str) -> str | None:
    m = _KEY_LITERAL.match(literal)
    if not m or f"{m.group(1)}:" in _SECRET_SCHEMES or m.group(2).isdigit():
        return None
    head = literal.split("{")[0]
    return head + "*" if "{" in literal or literal.endswith(":") else literal


def _scan_python(path: Path) -> tuple[set[str], set[str], set[str]]:
    tree = ast.parse(path.read_text())
    imports_redis = any(
        isinstance(n, (ast.Import, ast.ImportFrom)) and "redis" in ast.unparse(n) for n in ast.walk(tree)
    )
    commands: set[str] = set()
    channels: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            recv, method = ast.unparse(n.func.value), n.func.attr
            if recv in _CACHE_RECEIVERS and method in _CACHE_METHODS:
                commands.add(_CACHE_METHODS[method])
            elif method in _CLIENT_METHODS and (imports_redis or "redis" in recv):
                commands.add(_CLIENT_METHODS[method])
                commands.update(_EXTRA_COMMANDS.get(method, []))
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            if any(isinstance(t, ast.Name) and t.id.endswith("CHANNEL") for t in n.targets):
                channels.add(n.value.value)
    if not commands:
        return set(), set(), set()
    keys: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if (k := _key_pattern(n.value)):
                keys.add(k)
        elif isinstance(n, ast.JoinedStr) and n.values and isinstance(n.values[0], ast.Constant):
            if (k := _key_pattern(str(n.values[0].value) + "{")):
                keys.add(k)
        elif isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant):
            name = n.func.id if isinstance(n.func, ast.Name) else None
            if name in _KEY_BUILDERS and isinstance(n.args[0].value, str):
                keys.add(f"{n.args[0].value}:*")
    if not {"publish", "subscribe"} & commands:
        channels = set()
    return commands, keys, channels


def _scan_cpp(root: Path, paths: list[Path]) -> tuple[set[str], set[str], set[str]]:
    commands: set[str] = set()
    keys: set[str] = set()
    for p in paths:
        for f in sorted(p.rglob("*")) if p.is_dir() else [p]:
            if f.suffix not in {".cpp", ".h"}:
                continue
            text = f.read_text()
            if "redisCommand" not in text and "RedisClient" not in text:
                continue
            commands.update(c.lower() for c in _CPP_COMMAND.findall(text) if c != "AUTH")
            for lit in _CPP_KEY.findall(text):
                if (k := _key_pattern(lit)):
                    keys.add(k)
    return commands, keys, set()


def inventory(root: Path = ROOT, clients: dict[str, list[str]] = CLIENTS) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for name, rels in clients.items():
        paths = [Path(r) if Path(r).is_absolute() else root / r for r in rels]
        if name == "gateway":
            commands, keys, channels = _scan_cpp(root, paths)
        else:
            commands, keys, channels = set(), set(), set()
            for f in _py_files(paths):
                c, k, ch = _scan_python(f)
                commands |= c
                keys |= k
                channels |= ch
        out[name] = {"commands": sorted(commands), "keys": sorted(keys), "channels": sorted(channels)}
    return out


def uncovered(inv: dict[str, dict[str, list[str]]], acl_text: str) -> list[tuple[str, str, str]]:
    """Inventory entries the ACL file does not grant to that client's user."""
    users = {m.group(1): m.group(0) for m in re.finditer(r"^user (\S+) .*$", acl_text, re.M)}
    missing = []
    for client, entry in inv.items():
        line = users.get(client, "")
        granted = {
            "keys": set(re.findall(r"~([^\s()]+)", line)),
            "commands": {c.lower() for c in re.findall(r"\+([^\s()]+)", line)},
            "channels": set(re.findall(r"&([^\s()]+)", line)),
        }
        missing += [(client, kind, item) for kind in granted for item in entry[kind] if item not in granted[kind]]
    return missing


if __name__ == "__main__":
    json.dump(inventory(), sys.stdout, indent=2)
    print()
