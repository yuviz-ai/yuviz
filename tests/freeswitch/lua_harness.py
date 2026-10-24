"""Runs a FreeSWITCH Lua script under a stub `freeswitch`, `session` and `event`, and reports what it did."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

LUA = shutil.which("lua5.4") or shutil.which("lua")
requires_lua = pytest.mark.skipif(LUA is None, reason="BLOCKED-ENV: no lua interpreter installed")

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/freeswitch"


def _lit(value) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"[{_lit(k)}]={_lit(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "{" + ",".join(_lit(v) for v in value) + "}"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


_PRELUDE = """
local vars, headers, replies = %(vars)s, %(headers)s, %(replies)s
local function emit(kind, text) io.write(kind, "\\t", text, "\\n") end
local api = {}
function api:executeString(cmd)
    emit("API", cmd)
    for _, r in ipairs(replies) do
        if cmd:sub(1, #r[1]) == r[1] then return r[2] end
    end
    return "-ERR unstubbed"
end
function api:execute(name, args) return self:executeString(name .. " " .. args) end
freeswitch = {
    API = function() return api end,
    consoleLog = function(level, msg) emit("LOG", level .. ": " .. (msg:gsub("\\n$", ""))) end,
}
session = {
    getVariable = function(_, name) return vars[name] end,
    setVariable = function(_, name, value) emit("SETVAR", name .. "=" .. value) end,
    execute = function(_, app, data) emit("EXEC", app .. " " .. (data or "")) end,
    get_uuid = function() return vars.uuid end,
    answer = function() emit("ANSWER", "") end,
}
event = {getHeader = function(_, name) return headers[name] end}
argv = {}
dofile(%(script)s)
"""


def run(script: str, *, vars=None, headers=None, replies=()) -> list[tuple[str, str]]:
    """Runs scripts/freeswitch/<script>. `replies` maps an API command prefix to its answer (first match wins).
    Returns the (kind, text) actions in order: API commands, session variables set, applications executed, logs."""
    prelude = _PRELUDE % {"vars": _lit(vars or {}), "headers": _lit(headers or {}), "replies": _lit(list(replies)),
                          "script": _lit(str(SCRIPTS / script))}
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "run.lua"
        runner.write_text(prelude)
        proc = subprocess.run([LUA, str(runner)], capture_output=True, text=True, timeout=10, check=False)
    assert proc.returncode == 0, proc.stderr
    return [tuple(line.split("\t", 1)) for line in proc.stdout.splitlines()]
