"""
The cross-tenant TTS control (see the design's Data section and lesson 31):
CallFlowRunner.__init__'s `tts_config_id` keyword, sourced only from
CallFlow.resolved_tts_config_id, is the only way a call's voice may reach
the runtime. This grep tripwire fails if any statement in the callflow
package or __main__.py's handler_factory block reads `.tts_config_id` off a
node/graph object instead — the author-supplied, unvalidated value a
cross-tenant flow could otherwise drive another tenant's TTS engine and
secret through.
"""

from __future__ import annotations

import pathlib
import re

_CONVERSATION_DIR = pathlib.Path(__file__).resolve().parent.parent

# SetVoice's own field is legitimately named `tts_config_id` — reading it
# off an already-built Action (`action.tts_config_id` in handler.py) is
# reading the constructor's own value back, not a node/graph's.
_ALLOWED_OBJECTS = {"action"}


def _attribute_reads(text: str) -> list[str]:
    """Every `<object>.tts_config_id` attribute access — deliberately NOT
    matching `resolved_tts_config_id` (a different attribute name) or
    `self._tts_config_id` (a different, underscored attribute name)."""
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
    # Sanity check on the regex, not the feature under test — a graph-
    # resident read like `graph.start.tts_config_id` must be caught.
    assert _attribute_reads("voice = graph.start.tts_config_id") == ["start"]
    assert "start" not in _ALLOWED_OBJECTS
