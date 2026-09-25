"""
CallContextStore — services/cloudonix/handoff.py's HandoffStore, moved
here as-is (server-minted `secrets.token_urlsafe(32)`, claim-once, TTL,
capacity bound — see design Risks for why the WS admission token is
deliberately NOT `call.provider_call_id`), generalized with `provider`,
`account_ref` and `provider_call_id` on `CallRoute` so a multi-provider,
multi-account process can still route a claimed WS connection to the right
bridge parameters.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


class HandoffCapacityError(Exception):
    """Raised by issue() at _MAX_PENDING — resource exhaustion is not
    laundered through the routing fallback; the webhook turns this into a
    503 with no answer XML."""


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
        """Single-use pop; None if unknown or expired. The token is never
        derivable from provider_call_id — it is generated independently by
        issue() and is the only key this dict is ever looked up by, so a
        WS client presenting a provider's own call id in the token's place
        cannot admit any connection."""
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
    """The outbound-leg analogue of CallContextStore: `place_call()` calls
    `remember()` with the identity it already validated (via
    ownership.resolve_outbound_identity), keyed on the SAME
    `(provider, account_ref, idempotency_key)` that `answer_url`/`hangup_url`/
    `ring_url` carry back as `?idem=`. When the vendor calls the answer
    webhook for a call this service itself placed, `orchestrator` looks the
    identity up here and skips DID-based route resolution entirely — that
    resolution is for a genuinely inbound call, and running it against an
    outbound leg's callee number was two real bugs: it silently downgraded
    the answering agent to "default" (the outbound `agent_slug` was never
    consulted), and it 403'd with dead air whenever the dialled number
    happened to be provisioned as another tenant's DID.

    Not single-use (unlike CallContextStore's token claim): a vendor may
    retry a slow-to-ack answer webhook, and a second, identical lookup must
    still succeed rather than falling back to DID resolution on the retry.
    TTL matches the idempotency claim window so ringing has an equivalent
    grace period."""

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
            # Best-effort: a full map just means this one call falls back
            # to DID resolution, same as if it were never remembered.
            return
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
