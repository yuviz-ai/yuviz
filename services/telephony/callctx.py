"""CallContextStore — server-minted, claim-once, TTL'd WS admission tokens mapping to a CallRoute.
The token is deliberately independent of provider_call_id."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


class HandoffCapacityError(Exception):
    """Store at capacity; the webhook returns 503 rather than falling back."""


@dataclass(frozen=True)
class CallRoute:
    tenant_slug: str
    agent_slug: str
    caller_did: str
    called_did: str
    provider: str
    account_ref: str
    provider_call_id: str
    issued_at: float  # time.monotonic()
    direction: str = "inbound"


class CallContextStore:
    _TTL_S = 60.0
    _MAX_PENDING = 10_000

    def __init__(self) -> None:
        self._pending: dict[str, CallRoute] = {}

    def _evict_expired(self, now: float) -> None:
        expired = [tok for tok, route in self._pending.items() if now - route.issued_at >= self._TTL_S]
        for tok in expired:
            del self._pending[tok]

    def issue(self, route: CallRoute) -> str:
        now = time.monotonic()
        self._evict_expired(now)
        if len(self._pending) >= self._MAX_PENDING:
            raise HandoffCapacityError("call context store at capacity")
        token = secrets.token_urlsafe(32)
        self._pending[token] = route
        return token

    def claim(self, token: str) -> CallRoute | None:
        """Single-use pop; None if unknown or expired."""
        route = self._pending.pop(token, None)
        if route is None:
            return None
        if time.monotonic() - route.issued_at >= self._TTL_S:
            return None
        return route

    @property
    def pending_count(self) -> int:
        return len(self._pending)


@dataclass(frozen=True)
class OutboundRouteInfo:
    tenant_slug: str
    agent_slug: str
    remembered_at: float  # time.monotonic()


class OutboundIdentityStore:
    """Validated (tenant, agent) for calls we placed, keyed like the `?idem=` on answer/hangup/ring URLs,
    so the answer webhook skips DID resolution. Not single-use: vendors retry answer webhooks."""

    _TTL_S = 120.0
    _MAX_PENDING = 10_000

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], OutboundRouteInfo] = {}

    def _evict_expired(self, now: float) -> None:
        stale = [k for k, v in self._entries.items() if now - v.remembered_at >= self._TTL_S]
        for k in stale:
            del self._entries[k]

    def remember(self, provider: str, account_ref: str, idempotency_key: str, *, tenant_slug: str, agent_slug: str) -> None:
        now = time.monotonic()
        self._evict_expired(now)
        if len(self._entries) >= self._MAX_PENDING:
            raise HandoffCapacityError(f"outbound identity store at capacity ({self._MAX_PENDING})")
        self._entries[(provider, account_ref, idempotency_key)] = OutboundRouteInfo(
            tenant_slug=tenant_slug, agent_slug=agent_slug, remembered_at=now,
        )

    def recall(self, provider: str, account_ref: str, idempotency_key: str) -> tuple[str, str] | None:
        entry = self._entries.get((provider, account_ref, idempotency_key))
        if entry is None:
            return None
        if time.monotonic() - entry.remembered_at >= self._TTL_S:
            return None
        return entry.tenant_slug, entry.agent_slug


outbound_identities = OutboundIdentityStore()
