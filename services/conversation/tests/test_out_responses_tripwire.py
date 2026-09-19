"""Enumeration rule (lesson 29), not a hand list: every IConversationHandler
implementer must carry the explicit `out_responses` class attribute and a
real `on_dtmf`, or (respectively) ConversationSession.out_responses's
getattr(..., None) is the only thing standing between the no-flow majority
path and AttributeError, and a keypress silently vanishes into
push_dtmf()'s except clause. The implementer set is derived mechanically —
grep + AST, not a maintained list — so a future handler that forgets either
Protocol member fails at suite collection time, not mid-call."""

from __future__ import annotations

import ast
import importlib
import subprocess
from pathlib import Path

_CONVERSATION_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONVERSATION_DIR.parent.parent
_EXCLUDED = {"session.py", "servicer.py"}


def _implementer_modules() -> list[Path]:
    """`grep -rln "on_speech_ended" services/conversation`, minus the
    protocol/servicer modules — session.py declares the Protocol/dispatches
    to it, servicer.py only calls through ConversationSession, neither
    implements IConversationHandler itself."""
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
    """True only when __init__ assigns self.<attr> = ... *unconditionally* —
    i.e. as one of __init__'s own top-level statements, not nested inside an
    `if`/`try`/`for`/`while`/`with`. The CallFlowConversationHandler shape (a
    real queue built once per instance, never a class-level default) is
    exactly as safe at runtime as echo.py/pipeline.py's class-level
    `out_responses = None` ONLY if every construction path sets it —
    ConversationSession's getattr(..., None) has nothing to fall back to
    once a class attribute doesn't exist and an instance can be built
    without ever executing the assignment. Walking the whole subtree (the
    previous shape) would pass a branch-only assignment even though the
    assertion message calls this check "unconditional"."""
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
    """`IConversationHandler.on_speech_ended(self, session_id, audio,
    duration_ms, energy_db)` (see session.py) — matched on the first two
    non-self parameter names, not just the method name, so a same-named but
    differently-shaped method (fsm.py's ConversationFSM.on_speech_ended(self,
    duration_ms=0, energy_db=0.0) — an internal FSM transition, not a
    Protocol implementation) doesn't false-positive into this enumeration."""
    params = [a.arg for a in method.args.args][1:]  # drop self
    return params[:2] == ["session_id", "audio"]


def _implementer_classes() -> list[tuple[type, ast.ClassDef]]:
    """Resolves the grep hit list to actual classes, not modules: parses
    each hit with `ast` for a class that DEFINES a Protocol-shaped
    on_speech_ended directly in its own body (the mechanical test for "this
    is a real implementer", as opposed to a module that merely mentions the
    method name in a docstring/comment or defines an unrelated method of the
    same name — transfer_engine.py, transcript_builder.py, fsm.py and
    providers/stt/deepgram.py all match the grep but none defines a matching
    implementer), then imports the module and returns (class object, its
    AST node) pairs."""
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
    # The assertion message in test_out_responses_and_on_dtmf_present_on_
    # every_implementer says "unconditional self.out_responses = ...". A
    # helper that walks the whole __init__ subtree (ast.walk) would call
    # this class safe even though a construction path exists where the
    # attribute is never set — the exact AttributeError this feature's
    # own risk list warns about. Prove the helper actually distinguishes
    # the two shapes, or the enumeration's message is a claim it cannot
    # back up.
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
    # Sanity check on the grep + AST resolution, not the feature under test —
    # if this ever finds zero classes the enumeration below is vacuously
    # true and proves nothing.
    names = {cls.__name__ for cls, _ in _implementer_classes()}
    assert "EchoConversationHandler" in names
    assert "PipelineConversationHandler" in names


def test_out_responses_and_on_dtmf_present_on_every_implementer():
    for cls, node in _implementer_classes():
        # hasattr, not "in vars(cls)": a subclass (e.g. a test double built
        # on EchoConversationHandler) that inherits both members without
        # redeclaring them is exactly as safe at runtime as one that
        # declares them itself — attribute/method lookup follows the MRO,
        # which is the same resolution ConversationSession's getattr(...)
        # and ordinary attribute access use.
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
