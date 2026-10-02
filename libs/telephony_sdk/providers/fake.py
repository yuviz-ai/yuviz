"""Scriptable test-only telephony/SMS provider, registered hidden as "fake"."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

from ..exceptions import TelephonyProviderError
from ..interface import (
    InboundSyncResult, InboundUrls, ISmsProvider, ITelephonyProvider, NormalizedInboundCall, ReconcileResult,
)
from ..registry import SmsProviderRegistry, TelephonyProviderRegistry

_call_id_counter = itertools.count(1)
_message_id_counter = itertools.count(1)
_app_id_counter = itertools.count(1)

# Callers build a fresh instance per request, so tests read sync calls here.
SYNC_LOG: list[tuple[str, str, Any]] = []


class FakeProvider(ITelephonyProvider, ISmsProvider):
    PROVIDER_NAME = "fake"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)
        self.dial_count = 0
        self.send_count = 0
        # Scripted per instance via credentials, so instances never share state.
        self._initiate_should_timeout: bool = bool(credentials.get("initiate_should_timeout", False))
        self._initiate_should_fail: bool = bool(credentials.get("initiate_should_fail", False))
        self._reconcile_outcome: str = credentials.get("reconcile_outcome", "indeterminate")
        self._check_health_result: bool = bool(credentials.get("check_health_result", True))

    @classmethod
    def required_credential_fields(cls) -> list[str]:
        return []

    @classmethod
    def validate_credentials(cls, credentials: dict[str, Any]) -> None:
        return None

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        return []

    async def initiate_call(
        self, *, from_number: str, to_number: str,
        answer_url: str, hangup_url: str | None = None, ring_url: str | None = None,
    ) -> str:
        self.dial_count += 1
        if self._initiate_should_fail:
            raise TelephonyProviderError("fake: scripted initiate_call failure")
        if self._initiate_should_timeout:
            raise asyncio.TimeoutError("fake: scripted initiate_call timeout")
        return f"fake-call-{next(_call_id_counter)}"

    async def hangup_call(self, call_id: str) -> None:
        return None

    async def get_call_status(self, call_id: str) -> dict[str, Any]:
        return {"call_id": call_id, "status": "answered"}

    def verify_webhook_signature(self, url: str, headers: dict[str, str]) -> bool:
        return headers.get("x-fake-signature") == "valid"

    def normalize_inbound_webhook(
        self, *, url: str, headers: dict[str, str], fields: dict[str, Any],
        account_tenant_slug: str,
    ) -> NormalizedInboundCall:
        return NormalizedInboundCall(
            provider_call_id=str(fields.get("call_id", "")),
            from_number=str(fields.get("from", "")),
            to_number=str(fields.get("to", "")),
            known_tenant_slug=fields.get("known_tenant_slug"),
            raw=dict(fields),
        )

    def parse_dtmf_digit(self, fields: dict[str, Any]) -> str | None:
        digit = fields.get("digit") or fields.get("dtmf")
        return str(digit)[0] if digit else None

    def build_answer_response(self, websocket_url: str) -> str:
        return websocket_url

    async def check_health(self) -> bool:
        return self._check_health_result

    async def reconcile_call(
        self, *, reference: str, observed_call_id: str | None,
    ) -> ReconcileResult:
        if self._reconcile_outcome == "placed":
            return ReconcileResult(outcome="placed", provider_call_id=observed_call_id or f"fake-call-{reference}")
        if self._reconcile_outcome == "not_placed":
            return ReconcileResult(outcome="not_placed")
        return ReconcileResult(outcome="indeterminate")

    async def owns_number(self, number: str) -> bool | None:
        """Scripted by credentials["owns_number"]: "yes" (default), "no",
        "unknown" (None), or "error" (lookup failure)."""
        mode = self._credentials.get("owns_number", "yes")
        if mode == "error":
            raise TelephonyProviderError("fake: scripted ownership lookup failure")
        return {"yes": True, "no": False}.get(mode)

    async def attach_inbound(
        self, number: str, urls: InboundUrls, *, label: str, refresh_app: bool = True,
    ) -> InboundSyncResult:
        SYNC_LOG.append(("attach", number, urls))
        if not self._credentials.get("attach_ok", True):
            return InboundSyncResult(ok=False, message="fake: scripted attach failure")
        update = None
        if not self._credentials.get("inbound_application_id"):
            app_id = f"fake-app-{next(_app_id_counter)}"
            SYNC_LOG.append(("create_app", number, app_id))
            update = {"inbound_application_id": app_id}
        elif refresh_app:
            SYNC_LOG.append(("refresh_app", number, self._credentials["inbound_application_id"]))
        return InboundSyncResult(ok=True, credentials_update=update)

    async def refresh_inbound(self, urls: InboundUrls) -> InboundSyncResult | None:
        app_id = self._credentials.get("inbound_application_id")
        if not app_id:
            return None
        SYNC_LOG.append(("refresh_app", "", app_id))
        return InboundSyncResult(ok=True)

    async def discard_inbound_resources(self, credentials_update: dict[str, Any]) -> None:
        SYNC_LOG.append(("discard_app", "", credentials_update.get("inbound_application_id")))

    async def detach_inbound(self, number: str) -> InboundSyncResult:
        SYNC_LOG.append(("detach", number, None))
        if self._credentials.get("detach_ok", True):
            return InboundSyncResult(ok=True)
        return InboundSyncResult(ok=False, message="fake: scripted detach failure")

    async def send_sms(self, *, from_number: str, to_number: str, text: str) -> str:
        self.send_count += 1
        if self._initiate_should_fail:
            raise TelephonyProviderError("fake: scripted send_sms failure")
        return f"fake-message-{next(_message_id_counter)}"

    async def get_message_status(self, message_id: str) -> dict[str, Any]:
        return {"message_id": message_id, "status": "delivered"}


TelephonyProviderRegistry.register("fake", FakeProvider, hidden=True)
SmsProviderRegistry.register("fake", FakeProvider, hidden=True)
