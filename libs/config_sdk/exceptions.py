from __future__ import annotations


class ConfigSDKError(Exception):
    """Base class for all Config SDK errors."""


class RepositoryUnavailableError(ConfigSDKError):
    """Repository backend unreachable (distinct from "not found", which is None).

    CacheAsideConfigProvider catches this and falls through; it never escapes to callers."""
