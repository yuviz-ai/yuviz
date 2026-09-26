"""
ITelephonyProvider — the shared interface every telephony provider
(Vobiz today; Twilio/Telnyx additively later) implements.

Scope deliberately kept to exactly what a REST+webhook telephony provider's
own already-built, tested code needs (grounded in services/vobiz/'s real
client.py + signature.py, not a speculative superset copied from a richer
reference implementation): outbound call control, inbound webhook
verification, and the provider-specific "how do I tell you to start
streaming audio" response shape. It does NOT cover the long-lived
WebSocket/media-bridging side (that stays in services/vobiz/bridge.py,
vad.py, audio.py) — those are protocol/media concerns, not provider-config
concerns, exactly the same split Dograh's own ARI (direct-SIP) integration
draws between its request/response provider interface and its separate
long-lived channel-event process. Our own Gateway/Kamailio/FreeSWITCH path
is the equivalent of that separate process here and is deliberately never
made to implement this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from .exceptions import TelephonyTransferUnsupported


@dataclass(frozen=True)
class NormalizedInboundCall:
    provider_call_id: str
    from_number: str
    to_number: str
    known_tenant_slug: str | None  # filled by adapters whose account binds the tenant
    raw: dict[str, Any]


@dataclass(frozen=True)
class ReconcileResult:
    outcome: Literal["placed", "not_placed", "indeterminate"]
    provider_call_id: str | None = None


class ITelephonyProvider(ABC):
    """One instance per telephony_configs row — constructed with that row's
    `credentials` dict."""

    PROVIDER_NAME: str

    def __init__(self, credentials: dict[str, Any]) -> None:
        self._credentials = credentials

    @classmethod
    @abstractmethod
    def required_credential_fields(cls) -> list[str]:
        """Field names this provider needs in `credentials` — backs the
        Config Service's provider-discovery endpoint so an admin UI can
        render the right form without hardcoding per-provider fields."""

    @classmethod
    @abstractmethod
    def validate_credentials(cls, credentials: dict[str, Any]) -> None:
        """Raise TelephonyProviderError if credentials are missing/malformed.
        Called by Config Service at telephony_configs creation time, before
        the row is ever written — never at call time."""

    @abstractmethod
    async def initiate_call(
        self, *, from_number: str, to_number: str,
        answer_url: str, hangup_url: str | None = None, ring_url: str | None = None,
    ) -> str:
        """Places an outbound call, returns the provider's own call id."""

    @abstractmethod
    async def hangup_call(self, call_id: str) -> None:
        ...

    @abstractmethod
    async def get_call_status(self, call_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def verify_webhook_signature(self, url: str, headers: dict[str, str]) -> bool:
        """headers should already be lower-cased keys. Fail closed: a
        missing/invalid signature returns False, never raises past this
        point — the caller (a webhook route) turns False into a 403 before
        touching any call state."""

    @abstractmethod
    def build_answer_response(self, websocket_url: str) -> str:
        """The provider-specific XML/markup response to the answer webhook
        that tells the provider to open a media WebSocket to websocket_url."""

    @abstractmethod
    def normalize_inbound_webhook(
        self, *, url: str, headers: dict[str, str], fields: dict[str, Any],
        account_tenant_slug: str,
    ) -> NormalizedInboundCall:
        """Parses the vendor's inbound-call webhook shape into the
        provider-agnostic NormalizedInboundCall. Raises WebhookRejected for
        a vendor-specific rejection (Cloudonix's domain mismatch). Contains
        NO DID lookup and NO call-context work (AC9) — that is the
        orchestrator's job, not the adapter's."""

    @abstractmethod
    def parse_dtmf_digit(self, fields: dict[str, Any]) -> str | None:
        """Extracts DTMF digit from webhook payload; returns None if missing/invalid."""

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        """Field names in `credentials` this provider needs encrypted at
        rest — usable default (AC1): a provider with no secrets need not
        override this."""
        return []

    async def check_health(self) -> bool:
        """Cheap liveness probe against the vendor, called only from the
        health loop's own asyncio task — never on a request path. Usable
        default (AC3): a provider with no probe endpoint is always
        healthy."""
        return True

    async def transfer_call(self, *, call_id: str, destination: str) -> None:
        raise TelephonyTransferUnsupported(f"{self.PROVIDER_NAME}: transfer_call not yet supported")

    async def reconcile_call(
        self, *, reference: str, observed_call_id: str | None,
    ) -> ReconcileResult:
        """Default: if a callback already observed a vendor call id for this
        reference, confirm it with get_call_status() and report placed/
        not_placed; otherwise 'indeterminate'. A provider whose API can
        look up by our own reference overrides this. Never guesses
        'not_placed' (AC16/17)."""
        if observed_call_id is None:
            return ReconcileResult(outcome="indeterminate")
        try:
            await self.get_call_status(observed_call_id)
        except Exception:
            return ReconcileResult(outcome="indeterminate")
        return ReconcileResult(outcome="placed", provider_call_id=observed_call_id)


class ISmsProvider(ABC):
    PROVIDER_NAME: str

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        return []

    @abstractmethod
    async def send_sms(self, *, from_number: str, to_number: str, text: str) -> str:
        """Places an outbound SMS, returns the provider's own message id."""

    @abstractmethod
    async def get_message_status(self, message_id: str) -> dict[str, Any]:
        ...

    async def reconcile_message(
        self, *, reference: str, observed_message_id: str | None,
    ) -> ReconcileResult:
        if observed_message_id is None:
            return ReconcileResult(outcome="indeterminate")
        try:
            await self.get_message_status(observed_message_id)
        except Exception:
            return ReconcileResult(outcome="indeterminate")
        return ReconcileResult(outcome="placed", provider_call_id=observed_message_id)
