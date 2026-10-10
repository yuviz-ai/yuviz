"""FillerSelector: picks a tool-call filler phrase sized to the tool's calibrated
latency, avoiding the phrase just said."""

from __future__ import annotations

import logging
import math
import random

from .i18n import tool_fillers

log = logging.getLogger(__name__)

# English pool; other languages come from services/conversation/i18n.
_TOOL_FILLERS: tuple[tuple[str, float], ...] = tool_fillers("en")

# Uncalibrated tool: today's pool mid-length, not the 6s timeout ceiling.
_DEFAULT_TARGET_S = 1.6

_FALLBACK_FILLER = "One moment."


class FillerSelector:
    def select_tool_filler(
        self, tool_name: str, last_phrase: str | None, average_ms: float | None,
        language: str | None = None,
    ) -> str:
        """Never returns None — a tool call always gets a spoken filler.
        Returns _FALLBACK_FILLER if anything inside raises."""
        try:
            if average_ms is not None and math.isfinite(average_ms) and average_ms > 0:
                target = average_ms / 1000
            else:
                target = _DEFAULT_TARGET_S

            pool = tool_fillers(language) if language else _TOOL_FILLERS
            candidates = [p for p in pool if p[0] != last_phrase]
            if not candidates:
                candidates = list(pool)

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
