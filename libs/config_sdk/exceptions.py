from __future__ import annotations


class ConfigSDKError(Exception):
    """Base class for all Config SDK errors."""


class RepositoryUnavailableError(ConfigSDKError):
    """Repository backend unreachable (distinct from "not found", which is None)."""


class ConfigUnavailableError(ConfigSDKError):
    """No fresh config could be loaded and there is no last-known-good copy."""
