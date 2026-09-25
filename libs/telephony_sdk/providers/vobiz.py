"""
VobizTelephonyProvider — canonical home for Vobiz REST + webhook-signature
logic, moved here from services/vobiz/client.py + signature.py (which
originally held it before this SDK existed). Confirmed against Dograh's
real, working implementation (api/services/telephony/providers/vobiz/
provider.py) and verified live 2026-07-30/31 against the actual Vobiz API
(X-Auth-ID/X-Auth-Token headers, JSON body, phone numbers E.164 WITHOUT a
leading "+", call_uuid as the call identifier) and real Vobiz-signed
webhooks (V2/V3 HMAC-SHA256 signature scheme).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from ..exceptions import TelephonyProviderError
from ..interface import ISmsProvider, ITelephonyProvider, NormalizedInboundCall
from ..registry import SmsProviderRegistry, TelephonyProviderRegistry

_BASE_URL = "https://api.vobiz.ai/api"


def _base_url_no_query(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _expected_signature(auth_token: str, base_url: str, nonce: str, version: str) -> str:
    signed_payload = base_url + (f".{nonce}" if version == "v3" else nonce)
    digest = hmac.new(auth_token.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class VobizTelephonyProvider(ITelephonyProvider, ISmsProvider):
    PROVIDER_NAME = "vobiz"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)
        self._auth_id = credentials["auth_id"]
        self._auth_token = credentials["auth_token"]
        self._headers = {
            "X-Auth-ID": self._auth_id,
            "X-Auth-Token": self._auth_token,
            "Content-Type": "application/json",
        }

    @classmethod
    def required_credential_fields(cls) -> list[str]:
        return ["auth_id", "auth_token"]

    @classmethod
    def validate_credentials(cls, credentials: dict[str, Any]) -> None:
        missing = [f for f in cls.required_credential_fields() if not credentials.get(f)]
        if missing:
            raise TelephonyProviderError(f"Vobiz credentials missing required field(s): {missing}")

    async def initiate_call(
        self, *, from_number: str, to_number: str,
        answer_url: str, hangup_url: str | None = None, ring_url: str | None = None,
    ) -> str:
        """Numbers must be E.164 WITHOUT a leading "+" — Vobiz's own
        convention, confirmed in Dograh's implementation
        (`to_number.lstrip("+")`)."""
        body: dict[str, Any] = {
            "from": from_number.lstrip("+"),
            "to": to_number.lstrip("+"),
            "answer_url": answer_url,
            "answer_method": "POST",
        }
        if hangup_url:
            body["hangup_url"] = hangup_url
            body["hangup_method"] = "POST"
        if ring_url:
            body["ring_url"] = ring_url
            body["ring_method"] = "POST"

        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Call/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(endpoint, json=body, headers=self._headers)

        if resp.status_code != 201:
            raise TelephonyProviderError(f"Vobiz initiate_call returned {resp.status_code}: {resp.text}")

        data = resp.json()
        call_id = data.get("call_uuid") or data.get("CallUUID") or data.get("request_uuid")
        if not call_id:
            raise TelephonyProviderError(f"Vobiz initiate_call response missing call identifier: {data}")
        return call_id

    async def hangup_call(self, call_id: str) -> None:
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Call/{call_id}/"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(endpoint, headers=self._headers)

        if resp.status_code == 404:
            return
        if resp.status_code != 204:
            raise TelephonyProviderError(f"Vobiz hangup_call returned {resp.status_code}: {resp.text}")

    async def get_call_status(self, call_id: str) -> dict[str, Any]:
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Call/{call_id}/"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(endpoint, headers=self._headers)

        if resp.status_code != 200:
            raise TelephonyProviderError(f"Vobiz get_call_status returned {resp.status_code}: {resp.text}")
        return resp.json()

    def verify_webhook_signature(self, url: str, headers: dict[str, str]) -> bool:
        """Vobiz signs the callback's base URL (query parameters stripped)
        with the account auth_token and a per-request nonce.

            V2: base64(HMAC-SHA256(auth_token, base_url + nonce))
            V3: base64(HMAC-SHA256(auth_token, base_url + "." + nonce))

        Fail closed: a missing or forged signature must reject the callback
        before it can touch any call state."""
        signature = headers.get("x-vobiz-signature-v3") or headers.get("x-vobiz-signature-ma-v3")
        nonce = headers.get("x-vobiz-signature-v3-nonce")
        version = "v3"

        if not signature:
            signature = headers.get("x-vobiz-signature-v2") or headers.get("x-vobiz-signature-ma-v2")
            nonce = headers.get("x-vobiz-signature-v2-nonce")
            version = "v2"

        if not signature or not nonce:
            return False

        expected = _expected_signature(self._auth_token, _base_url_no_query(url), nonce, version)
        return hmac.compare_digest(expected, signature)

    def build_answer_response(self, websocket_url: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">{websocket_url}</Stream>'
            '</Response>'
        )

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        return ["auth_token"]

    def normalize_inbound_webhook(
        self, *, url: str, headers: dict[str, str], fields: dict[str, Any],
        account_tenant_slug: str,
    ) -> NormalizedInboundCall:
        """Vobiz's answer webhook is a form-encoded POST carrying
        CallUUID/To/From (case-tolerant, matching services/vobiz/app.py's
        existing `form.get("CallUUID") or form.get("call_uuid")` habit).
        known_tenant_slug is always None: unlike Cloudonix's per-account
        domain, a Vobiz account does not itself bind a tenant — the
        orchestrator falls back to account.tenant_slug."""
        lowered = {k.lower(): v for k, v in fields.items()}
        call_uuid = lowered.get("calluuid") or lowered.get("call_uuid") or ""
        to_number = lowered.get("to") or ""
        from_number = lowered.get("from") or ""
        return NormalizedInboundCall(
            provider_call_id=str(call_uuid),
            from_number=str(from_number),
            to_number=str(to_number),
            known_tenant_slug=None,
            raw=dict(fields),
        )

    async def check_health(self) -> bool:
        """GETs the account endpoint used by get_call_status/hangup_call —
        any 2xx/4xx response means the credentials at least reach Vobiz;
        only a transport failure or 5xx counts as unhealthy."""
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(endpoint, headers=self._headers)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

    async def send_sms(self, *, from_number: str, to_number: str, text: str) -> str:
        """Vobiz's Message API mirrors its Call API on the same auth
        headers (unverified live, see design Risks)."""
        body = {
            "from": from_number.lstrip("+"),
            "to": to_number.lstrip("+"),
            "text": text,
        }
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Message/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(endpoint, json=body, headers=self._headers)

        if resp.status_code != 201:
            raise TelephonyProviderError(f"Vobiz send_sms returned {resp.status_code}: {resp.text}")

        data = resp.json()
        message_id = data.get("message_uuid") or data.get("MessageUUID")
        if not message_id:
            raise TelephonyProviderError(f"Vobiz send_sms response missing message identifier: {data}")
        return message_id

    async def get_message_status(self, message_id: str) -> dict[str, Any]:
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Message/{message_id}/"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(endpoint, headers=self._headers)

        if resp.status_code != 200:
            raise TelephonyProviderError(f"Vobiz get_message_status returned {resp.status_code}: {resp.text}")
        return resp.json()


TelephonyProviderRegistry.register("vobiz", VobizTelephonyProvider)
SmsProviderRegistry.register("vobiz", VobizTelephonyProvider)
