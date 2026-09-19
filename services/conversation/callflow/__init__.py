"""
callflow — the IVR runtime for published call flows.

Sibling package to `workflow/`: `workflow/` is the conversational graph an
LLM-backed agent walks; `callflow/` is the deterministic, DTMF-driven graph
a caller walks before (optionally) being handed to one. See resolver.py,
runner.py and handler.py for the three pieces.
"""

from __future__ import annotations
