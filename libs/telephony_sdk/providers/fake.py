"""
FakeProvider — a deterministic, scriptable ITelephonyProvider + ISmsProvider
used only by tests. Registered hidden=True under "fake" in both registries
so it resolves by name for a test fixture but never appears in the Admin
UI's provider list (AC4). Also the only way to exercise the timeout/
ambiguity branches in AC13-17 without a real vendor.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

from ..exceptions import TelephonyProviderError
from ..interface import ISmsProvider, ITelephonyProvider, NormalizedInboundCall, ReconcileResult
from ..registry import SmsProviderRegistry, TelephonyProviderRegistry

_call_id_counter = itertools.count(1)
_message_id_counter = itertools.count(1)


class FakeProvider(ITelephonyProvider, ISmsProvider):
    PROVIDER_NAME = "fake"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)
        self.dial_count = 0
        self.send_count = 0
        # Scriptable via the credentials dict a test constructs this with —
        # not class-level, so two instances in the same test never share state.
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

    async def send_sms(self, *, from_number: str, to_number: str, text: str) -> str:
        self.send_count += 1
        if self._initiate_should_fail:
            raise TelephonyProviderError("fake: scripted send_sms failure")
        return f"fake-message-{next(_message_id_counter)}"

    async def get_message_status(self, message_id: str) -> dict[str, Any]:
        return {"message_id": message_id, "status": "delivered"}


TelephonyProviderRegistry.register("fake", FakeProvider, hidden=True)
SmsProviderRegistry.register("fake", FakeProvider, hidden=True)
