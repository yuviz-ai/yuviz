"""
CloudonixProvider — the credential-shape and answer-response half of the
Cloudonix integration (see .sdlc/cloudonix-telephony-provider/02-design.md).
Outbound call control is out of scope: this provider only exists so
Cloudonix's per-account credentials fit the same `telephony_configs`
convention Vobiz already uses (services/vobiz/app.py:90-114), which gives
the Admin UI a credential form for free.

No `app_id` field: R2-3 found that a tenant-writable free-form account
identifier could shadow another tenant's. The account's identity is its
`telephony_configs` row id, not anything in `credentials`.

`api_keys` entries must be `enc:` tokens ONLY — never `env:`/`k8s:`, never
a raw key. `telephony_configs.credentials` is tenant-writable JSONB, and
`CompositeSecretResolver`'s `env:`/`k8s:` schemes were built for
admin-entered infra config, not tenant input — see the design's "Account
key material" section for the arbitrary env-var/file-read this would
otherwise open.
"""

from __future__ import annotations

import hmac
from typing import Any
from xml.sax.saxutils import quoteattr

import httpx

from libs.config_sdk.secrets import is_encrypted

from ..exceptions import TelephonyProviderError, WebhookRejected
from ..interface import ITelephonyProvider, NormalizedInboundCall
from ..registry import TelephonyProviderRegistry

_MAX_API_KEYS = 10


class CloudonixProvider(ITelephonyProvider):
    PROVIDER_NAME = "cloudonix"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)
        self._domain = credentials["domain"]
        self._api_keys = tuple(credentials["api_keys"])
        # Optional — only needed for outbound.
        self._account_api_key = credentials.get("account_api_key")
        self._application_id = credentials.get("application_id")

    @classmethod
    def required_credential_fields(cls) -> list[str]:
        return ["domain", "api_keys"]

    @classmethod
    def validate_credentials(cls, credentials: dict[str, Any]) -> None:
        """Runs at telephony_configs creation/update time only, never at
        call time. Rejects any api_keys entry that is not an `enc:`
        token — `env:` and `k8s:` included."""
        domain = credentials.get("domain")
        if not domain or not isinstance(domain, str):
            raise TelephonyProviderError("cloudonix credentials missing required field: domain")

        api_keys = credentials.get("api_keys")
        if not isinstance(api_keys, list) or not api_keys:
            raise TelephonyProviderError("cloudonix credentials missing required field: api_keys")
        if len(api_keys) > _MAX_API_KEYS:
            raise TelephonyProviderError(f"cloudonix api_keys exceeds the {_MAX_API_KEYS}-entry limit")
        for entry in api_keys:
            if not isinstance(entry, str) or not is_encrypted(entry):
                raise TelephonyProviderError(
                    "cloudonix api_keys entries must be enc: tokens — send the key itself, "
                    "not an env:/k8s: reference"
                )

    async def initiate_call(
        self, *, from_number: str, to_number: str,
        answer_url: str, hangup_url: str | None = None, ring_url: str | None = None,
    ) -> str:
        """answer_url/hangup_url/ring_url unused — Cloudonix's webhook target
        is fixed per-application. Returns the token echoed back as CallSid."""
        if not self._account_api_key or not self._application_id:
            raise TelephonyProviderError(
                "cloudonix: outbound calling requires account_api_key and application_id credentials"
            )
        body = {"destination": to_number, "caller-id": from_number, "application": self._application_id}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://api.cloudonix.io/calls/{self._domain}/application",
                json=body, headers={"Authorization": f"bearer {self._account_api_key}"},
            )
        if resp.status_code >= 400:
            raise TelephonyProviderError(f"cloudonix: initiate_call failed {resp.status_code}: {resp.text}")
        data = resp.json()
        call_id = data.get("token")
        if not call_id:
            raise TelephonyProviderError(f"cloudonix: initiate_call response missing token: {data}")
        return call_id

    async def hangup_call(self, call_id: str) -> None:
        raise TelephonyProviderError("cloudonix: outbound call control out of scope")

    async def get_call_status(self, call_id: str) -> dict[str, Any]:
        raise TelephonyProviderError("cloudonix: outbound call control out of scope")

    def verify_webhook_signature(self, url: str, headers: dict[str, str]) -> bool:
        """Cloudonix sends a static X-CX-APIKey header rather than a
        signature. Compares the presented header against every entry of
        this account's api_keys, without short-circuit, skipping any entry
        that is still an is_encrypted() token (the instance was built from
        sealed or empty credentials, which must match nothing rather than
        raise). headers must already be lower-cased keys, per the abstract
        method's contract."""
        presented = headers.get("x-cx-apikey", "")
        matched = False
        for entry in self._api_keys:
            if is_encrypted(entry):
                continue
            if hmac.compare_digest(entry, presented):
                matched = True
        return matched

    def normalize_inbound_webhook(
        self, *, url: str, headers: dict[str, str], fields: dict[str, Any],
        account_tenant_slug: str,
    ) -> NormalizedInboundCall:
        """query ∪ JSON-or-form body, CallSid/To/From. known_tenant_slug is
        always the account's own tenant — a Cloudonix account binds a
        tenant by construction. A Domain field that disagrees with this
        account's own domain is a vendor-specific rejection (WebhookRejected),
        not a routing decision the orchestrator makes."""
        lowered = {k.lower(): v for k, v in fields.items()}
        domain = lowered.get("domain")
        if domain and str(domain).lower() != self._domain.lower():
            raise WebhookRejected(f"cloudonix: domain mismatch (got {domain!r})")

        call_id = lowered.get("callsid") or lowered.get("call_sid") or ""
        to_number = lowered.get("to") or ""
        from_number = lowered.get("from") or ""
        return NormalizedInboundCall(
            provider_call_id=str(call_id),
            from_number=str(from_number),
            to_number=str(to_number),
            known_tenant_slug=account_tenant_slug,
            raw=dict(fields),
        )

    def parse_dtmf_digit(self, fields: dict[str, Any]) -> str | None:
        lowered = {k.lower(): v for k, v in fields.items()}
        digit = lowered.get("digit") or lowered.get("dtmf") or lowered.get("dtmf_digit")
        return str(digit)[0] if digit else None

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        return ["api_keys", "account_api_key"]

    async def check_health(self) -> bool:
        """No probe endpoint exists for Cloudonix — usable default."""
        return True

    def build_answer_response(self, websocket_url: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Connect><Stream url={quoteattr(websocket_url)}/></Connect></Response>"
        )


TelephonyProviderRegistry.register("cloudonix", CloudonixProvider)
