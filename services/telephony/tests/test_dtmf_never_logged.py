"""Caller keypresses (PINs, card numbers) must never reach a log line, in any form.
Parses modules via AST so multi-line calls, other labels (`key=`) and plurals are caught."""
from __future__ import annotations

import ast
import re

import pytest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

# Every module a keypress passes through. Add new DTMF entry points here or this test can't see them.
_DTMF_ENTRY_POINTS = [
    _REPO / "libs" / "telephony_sdk",
    _REPO / "libs" / "media_stream_sdk",
    _REPO / "services" / "telephony",
    _REPO / "services" / "conversation",
]

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
# A string that labels a digit-named field, e.g. "digit=%s", "digits: {}".
_DIGIT_LABEL = re.compile(r"digits?\s*[=:]", re.IGNORECASE)


def _is_logger(node: ast.expr) -> bool:
    """`log`, `logger`, `_log`, `LOG`, `logging`, `self.log`, `self._logger`, …"""
    if isinstance(node, ast.Name):
        return "log" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "log" in node.attr.lower()
    return False


def _log_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _LOG_METHODS
        and _is_logger(n.func.value)
    ]


def _leaks_a_digit(call: ast.Call) -> bool:
    for arg in [*call.args, *(kw.value for kw in call.keywords)]:
        for node in ast.walk(arg):
            if isinstance(node, ast.Name) and "digit" in node.id.lower():
                return True
            if isinstance(node, ast.Attribute) and "digit" in node.attr.lower():
                return True
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _DIGIT_LABEL.search(node.value):
                return True
    return False


def _modules(root: Path) -> list[Path]:
    return [
        p for p in sorted(root.rglob("*.py"))
        if "tests" not in p.parts and "__pycache__" not in p.parts
    ]


def test_the_entry_points_exist_and_contain_log_calls():
    # Guard against a vacuous pass if a directory moves.
    for root in _DTMF_ENTRY_POINTS:
        assert root.is_dir(), f"{root} is gone — update _DTMF_ENTRY_POINTS"
        scanned = sum(len(_log_calls(p)) for p in _modules(root))
        assert scanned > 0, f"no log calls found under {root}; the scan is not looking anywhere"


def test_no_log_call_passes_a_dtmf_digit():
    leaks = [
        f"{path.relative_to(_REPO)}:{call.lineno}: {ast.unparse(call)}"
        for root in _DTMF_ENTRY_POINTS
        for path in _modules(root)
        for call in _log_calls(path)
        if _leaks_a_digit(call)
    ]
    assert not leaks, (
        "a caller keypress is written to a log line — a PIN entered at a "
        "`collect` node would be recoverable from logs. Log presence only:\n  "
        + "\n  ".join(leaks)
    )


# The detector itself, against the shapes a line-based scan let through.
_LEAKY = [
    'log.info("dtmf received call=%s digit=%s", cid, digit)',
    'self.log.info(\n    "dtmf received call=%s digit=%s",\n    cid,\n    digit,\n)',
    'log.info("dtmf received call=%s key=%s", cid, digit)',
    'log.info("telephony.dtmf.sent digits=%s", dtmf_digit)',
    'logger.debug(f"pressed {event.digit}")',
    'log.info("collected %d keys", len(digits))',
    'logging.warning("digits: %s", buf)',
]
_CLEAN = [
    'log.info("dtmf received call=%s", cid)',
    'log.info("collect node %s complete", node_id)',
    'self.log.debug("dtmf ignored: no live session")',
]


def test_the_detector_flags_the_known_leak_shapes():
    for src in _LEAKY:
        calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)]
        assert any(_leaks_a_digit(c) for c in calls), f"not flagged: {src!r}"
    for src in _CLEAN:
        calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)]
        assert not any(_leaks_a_digit(c) for c in calls), f"false positive: {src!r}"


# The C++ Gateway is a DTMF entry point too: FreeSWITCH DTMF events
# (EslEventListener) and the gRPC hop to Conversation (send_dtmf).
_CPP_DTMF_ENTRY_POINTS = [_REPO / "gateway"]
_CPP_LOG_CALL = re.compile(r"\b\w*log\w*\s*(?:\.|->)\s*(?:trace|debug|info|warn|error|critical)\s*\(", re.IGNORECASE)


def _cpp_log_calls(path: Path) -> list[tuple[int, str]]:
    """(line, full call text) for each logger call, spanning lines to its closing paren."""
    src = path.read_text()
    calls = []
    for m in _CPP_LOG_CALL.finditer(src):
        depth, end = 0, len(src)
        for j in range(m.end() - 1, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        calls.append((src.count("\n", 0, m.start()) + 1, src[m.start():end]))
    return calls


def _cpp_leaks_a_digit(call: str) -> bool:
    return bool(_DIGIT_LABEL.search(call) or re.search(r"\bdigits?\b", call, re.IGNORECASE))


def _cpp_sources() -> list[Path]:
    return [
        p for root in _CPP_DTMF_ENTRY_POINTS for p in sorted(root.rglob("*"))
        if p.suffix in {".cpp", ".h", ".hpp"} and "tests" not in p.parts
    ]


def test_the_gateway_sources_exist_and_contain_log_calls():
    assert sum(len(_cpp_log_calls(p)) for p in _cpp_sources()) > 0, "the C++ scan is not looking anywhere"


def test_no_gateway_log_call_passes_a_dtmf_digit():
    leaks = [
        f"{path.relative_to(_REPO)}:{line}: {' '.join(call.split())}"
        for path in _cpp_sources()
        for line, call in _cpp_log_calls(path)
        if _cpp_leaks_a_digit(call)
    ]
    assert not leaks, "a caller keypress is written to a Gateway log line. Log presence only:\n  " + "\n  ".join(leaks)


@pytest.mark.parametrize("call", [
    'logger_.info("EslEventListener: DTMF uuid={} digit={}", uuid, digit);',
    'logger_.warn("GrpcTransport: dropping dtmf session={}\\n"\n    " key={}", session_id,\n    digit);',
    'log_->debug("pressed {}", ev.digits);',
])
def test_the_cpp_detector_catches_leaky_shapes(call):
    (_, text), = _cpp_log_calls_from(call)
    assert _cpp_leaks_a_digit(text)


@pytest.mark.parametrize("call", [
    'logger_.info("EslEventListener: DTMF received uuid={}", uuid);',
    'logger_.warn("GrpcTransport: send_queue_ full, dropping dtmf session={}", session_id);',
])
def test_the_cpp_detector_passes_presence_only_lines(call):
    (_, text), = _cpp_log_calls_from(call)
    assert not _cpp_leaks_a_digit(text)


def _cpp_log_calls_from(src: str) -> list[tuple[int, str]]:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as f:
        f.write(src)
    try:
        return _cpp_log_calls(Path(f.name))
    finally:
        Path(f.name).unlink()
