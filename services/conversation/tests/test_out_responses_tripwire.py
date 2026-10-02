"""Every IConversationHandler implementer (found via grep + AST, not a hand list) must define
`out_responses` and a real `on_dtmf`."""

from __future__ import annotations

import ast
import importlib
import subprocess
from pathlib import Path

_CONVERSATION_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONVERSATION_DIR.parent.parent
_EXCLUDED = {"session.py", "servicer.py"}


def _implementer_modules() -> list[Path]:
    """Modules mentioning on_speech_ended, excluding the session/servicer protocol modules."""
    out = subprocess.run(
        ["grep", "-rl", "on_speech_ended", str(_CONVERSATION_DIR)],
        capture_output=True, text=True, check=False,
    ).stdout
    modules = []
    for line in out.splitlines():
        path = Path(line)
        if path.suffix != ".py" or "__pycache__" in path.parts:
            continue
        if path.name in _EXCLUDED:
            continue
        modules.append(path)
    return modules


def _dotted_module_name(path: Path) -> str:
    rel = path.resolve().relative_to(_REPO_ROOT)
    return ".".join(rel.with_suffix("").parts)


def _sets_instance_attr(class_node: ast.ClassDef, attr: str) -> bool:
    """True only when __init__ assigns self.<attr> as a top-level statement (not inside if/try/for/while/with)."""
    init = next(
        (n for n in class_node.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"),
        None,
    )
    if init is None:
        return False
    for node in init.body:  # top-level statements only — no ast.walk
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == attr and isinstance(t.value, ast.Name) and t.value.id == "self":
                return True
    return False


def _is_protocol_shaped(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Match on the first two non-self param names so same-named methods (e.g. fsm.py's) don't false-positive."""
    params = [a.arg for a in method.args.args][1:]  # drop self
    return params[:2] == ["session_id", "audio"]


def _implementer_classes() -> list[tuple[type, ast.ClassDef]]:
    """Classes that define a Protocol-shaped on_speech_ended in their own body, as (class, AST node) pairs."""
    classes: list[tuple[type, ast.ClassDef]] = []
    for module_path in _implementer_modules():
        tree = ast.parse(module_path.read_text())
        class_nodes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "on_speech_ended" and _is_protocol_shaped(n)
                for n in node.body
            )
        ]
        if not class_nodes:
            continue
        module = importlib.import_module(_dotted_module_name(module_path))
        for node in class_nodes:
            classes.append((getattr(module, node.name), node))
    return classes


def test_sets_instance_attr_rejects_a_conditional_assignment():
    # The helper must reject an assignment that only happens on some construction paths.
    conditional_src = (
        "class C:\n"
        "    def __init__(self, flag):\n"
        "        if flag:\n"
        "            self.out_responses = None\n"
    )
    unconditional_src = (
        "class C:\n"
        "    def __init__(self, flag):\n"
        "        self.out_responses = None\n"
    )
    conditional_node = ast.parse(conditional_src).body[0]
    unconditional_node = ast.parse(unconditional_src).body[0]
    assert _sets_instance_attr(conditional_node, "out_responses") is False
    assert _sets_instance_attr(unconditional_node, "out_responses") is True


def test_every_implementer_module_defines_on_speech_ended_and_a_handler_class():
    # If this finds zero classes, the enumeration below is vacuously true.
    names = {cls.__name__ for cls, _ in _implementer_classes()}
    assert "EchoConversationHandler" in names
    assert "PipelineConversationHandler" in names


def test_out_responses_and_on_dtmf_present_on_every_implementer():
    for cls, node in _implementer_classes():
        # hasattr, not vars(cls): inherited members are just as safe via MRO lookup.
        has_out_responses = hasattr(cls, "out_responses") or _sets_instance_attr(node, "out_responses")
        assert has_out_responses, (
            f"{cls.__name__} is missing out_responses (neither a class "
            "attribute nor an unconditional self.out_responses = ... in "
            "__init__) — ConversationSession.out_responses would raise "
            "AttributeError on this handler instead of reading None"
        )
        assert hasattr(cls, "on_dtmf"), (
            f"{cls.__name__} is missing on_dtmf — a keypress would vanish "
            "into ConversationSession.push_dtmf()'s except clause instead of "
            "reaching this handler"
        )
