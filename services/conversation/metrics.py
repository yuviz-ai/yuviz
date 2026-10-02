"""Minimal counter/histogram interface mirroring the gateway's IMetrics.
NullMetrics is the default; metrics are opt-in."""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class IMetrics(Protocol):
    def increment(self, name: str, value: float = 1.0) -> None: ...
    def observe(self, name: str, value: float) -> None: ...


class NullMetrics:
    """Default sink — every call is a no-op."""

    def increment(self, name: str, value: float = 1.0) -> None:
        pass

    def observe(self, name: str, value: float) -> None:
        pass


class LoggingMetrics:
    """Dev/debug sink that logs every emission."""

    def increment(self, name: str, value: float = 1.0) -> None:
        log.info("metric increment name=%s value=%s", name, value)

    def observe(self, name: str, value: float) -> None:
        log.info("metric observe name=%s value=%s", name, value)
