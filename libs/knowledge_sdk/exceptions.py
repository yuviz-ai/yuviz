from __future__ import annotations


class KnowledgeSDKError(Exception):
    """Base class for all Knowledge SDK errors."""


class RepositoryUnavailableError(KnowledgeSDKError):
    """Repository backend unreachable; the provider degrades this to "no context" (None)."""
