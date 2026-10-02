"""
Tripwire: the voice must come only from the validated resolved_tts_config_id, never
`.tts_config_id` off a node/graph (unvalidated; could select another tenant's TTS secret).
"""

from __future__ import annotations

import pathlib
import re

_CONVERSATION_DIR = pathlib.Path(__file__).resolve().parent.parent

# action.tts_config_id reads back the already-validated constructor value.
_ALLOWED_OBJECTS = {"action"}


def _attribute_reads(text: str) -> list[str]:
    """Objects of every `<obj>.tts_config_id` read (not resolved_/underscored variants)."""
    return [m.group(1) for m in re.finditer(r"(\w+)\.tts_config_id\b", text)]


def _files() -> list[pathlib.Path]:
    return [
        *sorted((_CONVERSATION_DIR / "callflow").glob("*.py")),
        _CONVERSATION_DIR / "__main__.py",
    ]


def test_no_tts_config_id_read_off_node_or_graph_object():
    violations = []
    for path in _files():
        for obj in _attribute_reads(path.read_text()):
            if obj not in _ALLOWED_OBJECTS:
                violations.append(f"{path.name}: {obj}.tts_config_id")
    assert not violations, (
        "tts_config_id read off a node/graph object instead of the "
        f"validated constructor value: {violations}"
    )


def test_tripwire_itself_can_fail():
    assert _attribute_reads("voice = graph.start.tts_config_id") == ["start"]
    assert "start" not in _ALLOWED_OBJECTS
