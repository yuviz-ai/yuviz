"""FillerSelector: picks a tool-call filler phrase sized to the tool's calibrated
latency, avoiding the phrase just said."""

from __future__ import annotations

import logging
import math
import random

log = logging.getLogger(__name__)

# (phrase, approx spoken seconds); lengths are estimates, not measured.
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

            # Random rather than a counter: the selector is shared by all concurrent calls.
            return random.choice(tier)[0]
        except Exception:
            log.exception("FillerSelector.select_tool_filler failed tool=%r", tool_name)
            return _FALLBACK_FILLER
