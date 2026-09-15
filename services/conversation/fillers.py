"""
FillerSelector — owns all user-facing filler wording, out of pipeline.py.
Answers one question for pipeline.py: which tool-call filler fits this
tool's calibrated average and isn't the phrase we just said. Tool-call
fillers apply from turn 1 onward — see pipeline.py's ToolCallStartedEvent
handling for why silence during a real tool call reads as a dropped call
no matter how early in the conversation it happens. This selector has no
notion of turns at all; the only suppression is pipeline.py's
_TOOL_CALL_FILLER_MIN_GAP_S burst gap.

The public method is total (never raises) — see its own docstring for
its specific fallback.
"""

from __future__ import annotations

import logging
import math
import random

log = logging.getLogger(__name__)

# Spoken while a tool call is in flight, sized against ToolLatencyStore's
# calibrated average for that (tenant, agent, tool). approx_seconds is a
# declared, not measured, spoken length — see design's Risks section on
# why a rough per-phrase estimate is good enough here.
_TOOL_FILLERS: tuple[tuple[str, float], ...] = (
    ("One moment.", 1.0),
    ("Just a second.", 1.0),
    ("Let me check on that.", 1.5),
    ("Give me a moment.", 1.5),
    ("Sure, let me look into that.", 2.0),
    ("One moment while I take care of that.", 2.4),
)

# Uncalibrated tool: today's pool mid-length, not the 6s timeout ceiling.
_DEFAULT_TARGET_S = 1.6

_FALLBACK_FILLER = "One moment."


class FillerSelector:
    def select_tool_filler(
        self, tool_name: str, last_phrase: str | None, average_ms: float | None,
    ) -> str:
        """Never returns None — a tool call always gets a spoken filler.
        Returns _FALLBACK_FILLER if anything inside raises."""
        try:
            if average_ms is not None and math.isfinite(average_ms) and average_ms > 0:
                target = average_ms / 1000
            else:
                target = _DEFAULT_TARGET_S

            candidates = [p for p in _TOOL_FILLERS if p[0] != last_phrase]
            if not candidates:
                candidates = list(_TOOL_FILLERS)

            eligible = [p for p in candidates if p[1] <= target]
            if eligible:
                best = max(p[1] for p in eligible)
                tier = [p for p in eligible if p[1] == best]
            else:
                shortest = min(p[1] for p in candidates)
                tier = [p for p in candidates if p[1] == shortest]

            # random, not a rotating counter: a counter on a FillerSelector
            # shared by every concurrent call across every tenant (see
            # __main__.py) isn't actually "this call's rotation" — two
            # callers in flight interleave increments, so what looked like
            # deterministic variety was really arbitrary anyway. Random
            # selection is honest about that and needs no shared state.
            return random.choice(tier)[0]
        except Exception:
            log.exception("FillerSelector.select_tool_filler failed tool=%r", tool_name)
            return _FALLBACK_FILLER
