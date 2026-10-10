from __future__ import annotations


class ConfigSDKError(Exception):
    """Base class for all Config SDK errors."""


class RepositoryUnavailableError(ConfigSDKError):
    """Repository backend unreachable (distinct from "not found", which is None).
    transient=False marks a rejected request (bad input), which says nothing about the backend."""

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class ConfigUnavailableError(ConfigSDKError):
    """No fresh config could be loaded and there is no last-known-good copy."""
