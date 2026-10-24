"""Exceptions shared by every ITelephonyProvider implementation — mirrors
libs/knowledge_sdk/exceptions.py's shape (one narrow exception type per SDK,
not a generic Exception a caller has to guess at)."""

from __future__ import annotations


class TelephonyProviderError(Exception):
    """Raised for any provider-level failure (auth, malformed request,
    unreachable, invalid credentials) — never for a normal call outcome
    (busy/no-answer arrive as status callbacks, not exceptions)."""


class TelephonyTransferUnsupported(TelephonyProviderError):
    """Raised by the default transfer_call() — a provider that supports a
    real cold/warm transfer overrides the method instead of catching
    this."""


class WebhookRejected(TelephonyProviderError):
    """Raised by normalize_inbound_webhook() for a vendor-specific
    rejection (e.g. Cloudonix's domain mismatch) — the orchestrator turns
    this into a 403 before any call-context work."""


class TelephonyRateLimited(TelephonyProviderError):
    """Raised when the provider answers 429; `retry_after` is the provider's
    Retry-After in seconds, or None when it sent none."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
