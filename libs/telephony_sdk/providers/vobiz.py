"""Vobiz REST client and webhook-signature verification (V2/V3 HMAC-SHA256)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx

from ..exceptions import TelephonyProviderError, TelephonyRateLimited
from ..interface import (
    ISmsProvider, ITelephonyProvider, InboundSyncResult, InboundUrls, NormalizedInboundCall, ReconcileResult,
)
from ..registry import SmsProviderRegistry, TelephonyProviderRegistry

log = logging.getLogger(__name__)

_BASE_URL = "https://api.vobiz.ai/api"
# Vobiz ignores call-list filters, so reconcile pages through calls itself, within these bounds.
_RECONCILE_SETTLE_S = 30  # a create request Vobiz got after our timeout can still dial
_RECONCILE_SKEW_S = 60
_RECONCILE_PAGE = 20
_RECONCILE_MAX_PAGES = 10
_RECONCILE_MAX_OPEN = 25


def _base_url_no_query(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _e164(number: str) -> str:
    """Vobiz's number inventory keys numbers as E.164 with a leading "+"."""
    digits = "".join(c for c in number if c.isdigit())
    return f"+{digits}"


def _digits(number: Any) -> str:
    return "".join(c for c in str(number or "") if c.isdigit())


def _vobiz_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200] or "no details"
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or body)[:200]
    return str(body)[:200]


def _expected_signature(auth_token: str, base_url: str, nonce: str, version: str) -> str:
    signed_payload = base_url + (f".{nonce}" if version == "v3" else nonce)
    digest = hmac.new(auth_token.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


# Vobiz reports a live call in call_state and an ended one only through answer_time and hangup_cause.
_LIVE_STATES = {"queued": "queued", "ringing": "ringing", "early": "ringing", "in-progress": "in-progress",
                "answered": "in-progress"}
_UNANSWERED_CAUSES = {
    "USER_BUSY": "busy",
    "NO_ANSWER": "no-answer", "NO_USER_RESPONSE": "no-answer", "ALLOTTED_TIMEOUT": "no-answer",
    "CALL_REJECTED": "rejected", "ORIGINATOR_CANCEL": "canceled",
}


def _call_status(data: dict[str, Any]) -> str:
    state = str(data.get("call_state") or "").lower()
    if state in _LIVE_STATES:
        return _LIVE_STATES[state]
    cause = str(data.get("hangup_cause") or "").upper()
    if not cause and not data.get("end_time"):
        return state or "unknown"
    if data.get("answer_time"):
        return "completed"
    return _UNANSWERED_CAUSES.get(cause, "failed")


class VobizTelephonyProvider(ITelephonyProvider, ISmsProvider):
    PROVIDER_NAME = "vobiz"
    supports_campaign_outbound = True
    # Tests replace this with an httpx.MockTransport.
    _http_transport: httpx.AsyncBaseTransport | None = None

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
        """Vobiz's Call API wants E.164 without the leading "+"."""
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
        async with self._client() as client:
            resp = await client.post(endpoint, json=body, headers=self._headers)

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            raise TelephonyRateLimited(
                "Rate limited by Vobiz", retry_after=int(retry_after) if retry_after.isdigit() else None,
            )
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
        """Vobiz's call record, plus the `status`/`to`/`from` keys every provider returns."""
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Call/{call_id}/"
        async with httpx.AsyncClient(timeout=10.0, transport=self._http_transport) as client:
            resp = await client.get(endpoint, headers=self._headers)

        if resp.status_code != 200:
            raise TelephonyProviderError(f"Vobiz get_call_status returned {resp.status_code}: {resp.text}")
        data = resp.json()
        return {**data, "status": _call_status(data), "to": data.get("to_number"), "from": data.get("from_number")}

    async def reconcile_call(
        self, *, reference: str, observed_call_id: str | None,
        to: str | None = None, from_number: str | None = None, created_after: datetime | None = None,
    ) -> ReconcileResult:
        """'not_placed' only when no queued, live or recorded call from `from_number` to `to` exists since `created_after`."""
        if observed_call_id is not None or not (to and from_number and created_after):
            return await super().reconcile_call(reference=reference, observed_call_id=observed_call_id)
        if not (to.startswith("+") and from_number.startswith("+")) or created_after.tzinfo is None:
            return ReconcileResult(outcome="indeterminate")
        if (datetime.now(timezone.utc) - created_after).total_seconds() < _RECONCILE_SETTLE_S:
            return ReconcileResult(outcome="indeterminate")
        target = (_digits(to), _digits(from_number))
        try:
            async with self._client() as client:
                open_matches = await self._open_calls_to(client, target)
                recorded = await self._recorded_calls_to(client, target, created_after) if open_matches == [] else None
        except (httpx.HTTPError, TelephonyProviderError, KeyError, TypeError, ValueError) as exc:
            log.warning("Vobiz reconcile failed reference=%s: %s", reference, type(exc).__name__)
            return ReconcileResult(outcome="indeterminate")
        matches = open_matches or recorded
        if matches is None or len(matches) > 1:
            return ReconcileResult(outcome="indeterminate")
        if matches:
            return ReconcileResult(outcome="placed", provider_call_id=matches[0])
        return ReconcileResult(outcome="not_placed")

    async def _open_calls_to(self, client: httpx.AsyncClient, target: tuple[str, str]) -> list[str] | None:
        """Queued and live calls to the target; None when there are too many to check or a detail lacks to/from."""
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Call/"
        matches: list[str] = []
        for status in ("queued", "live"):
            resp = await client.get(endpoint, params={"status": status}, headers=self._headers)
            if resp.status_code != 200:
                raise TelephonyProviderError(f"Vobiz {status} call list returned {resp.status_code}")
            call_ids = resp.json()["calls"]
            if not isinstance(call_ids, list) or len(call_ids) > _RECONCILE_MAX_OPEN:
                return None
            for call_id in call_ids:
                detail = await client.get(f"{endpoint}{call_id}/", params={"status": status}, headers=self._headers)
                if detail.status_code == 404:
                    continue
                if detail.status_code != 200:
                    raise TelephonyProviderError(f"Vobiz {status} call detail returned {detail.status_code}")
                data = detail.json()
                to_number, from_value = data.get("to_number") or data.get("to"), data.get("from_number") or data.get("from")
                if not to_number or not from_value:
                    return None
                if (_digits(to_number), _digits(from_value)) == target:
                    matches.append(str(call_id))
        return matches

    async def _recorded_calls_to(
        self, client: httpx.AsyncClient, target: tuple[str, str], since: datetime,
    ) -> list[str] | None:
        """Ended calls to the target since `since`, walking newest-first; None if that order can't be confirmed."""
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/Call/"
        floor = since - timedelta(seconds=_RECONCILE_SKEW_S)
        matches: list[str] = []
        offset, previous = 0, None
        for _ in range(_RECONCILE_MAX_PAGES):
            resp = await client.get(
                endpoint, params={"limit": _RECONCILE_PAGE, "offset": offset}, headers=self._headers,
            )
            if resp.status_code != 200:
                raise TelephonyProviderError(f"Vobiz call list returned {resp.status_code}")
            body = resp.json()
            records, total = body["objects"], int(body["meta"]["total_count"])
            for index, record in enumerate(records):
                started = datetime.fromisoformat(record.get("initiation_time") or record["created_at"])
                if started.tzinfo is None or (previous is not None and started > previous):
                    return None
                # Stop early only once a newer record before this one shows the list runs newest-first.
                if started < floor and (previous is not None or offset + index + 1 >= total):
                    return matches
                previous = started
                if started < floor:
                    continue
                if (_digits(record.get("to_number")), _digits(record.get("from_number"))) == target:
                    matches.append(str(record["call_uuid"]))
            offset += len(records)
            if offset >= total:
                return matches
            if not records:
                return None
        return None

    def verify_webhook_signature(self, url: str, headers: dict[str, str]) -> bool:
        """Vobiz signs the callback's base URL (query parameters stripped)
        with the account auth_token and a per-request nonce.

            V2: base64(HMAC-SHA256(auth_token, base_url + nonce))
            V3: base64(HMAC-SHA256(auth_token, base_url + "." + nonce))
        """
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
        """Case-tolerant CallUUID/To/From. known_tenant_slug is None: a Vobiz account binds no tenant."""
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

    def parse_dtmf_digit(self, fields: dict[str, Any]) -> str | None:
        lowered = {k.lower(): v for k, v in fields.items()}
        digit = lowered.get("digit") or lowered.get("dtmf") or lowered.get("digits")
        return str(digit)[0] if digit else None

    async def check_health(self) -> bool:
        """Only a transport failure or 5xx counts as unhealthy."""
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(endpoint, headers=self._headers)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15.0, transport=self._http_transport)

    async def owns_number(self, number: str) -> bool | None:
        target = _e164(number)
        endpoint = f"{_BASE_URL}/v1/Account/{self._auth_id}/numbers"
        page = 1
        try:
            async with self._client() as client:
                while True:
                    resp = await client.get(
                        endpoint, params={"page": page, "per_page": 100, "search": target}, headers=self._headers,
                    )
                    if resp.status_code != 200:
                        raise TelephonyProviderError(f"Vobiz number lookup returned {resp.status_code}")
                    data = resp.json()
                    items = data.get("items") or []
                    if any(item.get("e164") == target for item in items):
                        return True
                    per_page = int(data.get("per_page") or 100)
                    if not items or page * per_page >= int(data.get("total") or 0):
                        return False
                    page += 1
        except httpx.HTTPError as exc:
            raise TelephonyProviderError(f"couldn't reach Vobiz: {exc}") from exc

    async def attach_inbound(
        self, number: str, urls: InboundUrls, *, label: str, refresh_app: bool = True,
    ) -> InboundSyncResult:
        """Bind the number to this config's Application, creating it on first use.
        Refreshing an existing Application's URLs repairs every sibling number."""
        target = _e164(number)
        app_fields = {
            "answer_url": urls.answer_url, "answer_method": "POST",
            "hangup_url": urls.hangup_url, "hangup_method": "POST",
        }
        account = f"{_BASE_URL}/v1/Account/{self._auth_id}"
        app_id = self._credentials.get("inbound_application_id")
        created: dict[str, Any] | None = None
        try:
            async with self._client() as client:

                async def create_app() -> tuple[str | None, str | None]:
                    resp = await client.post(f"{account}/Application/", json={"app_name": label, **app_fields}, headers=self._headers)
                    if resp.status_code not in (200, 201):
                        return None, f"Vobiz didn't create the application (HTTP {resp.status_code}: {_vobiz_error(resp)})"
                    new_id = str(resp.json().get("app_id") or "")
                    return (new_id, None) if new_id else (None, "Vobiz created the application but returned no id")

                if not app_id:
                    app_id, error = await create_app()
                    if not app_id:
                        return InboundSyncResult(ok=False, message=error)
                    created = {"inbound_application_id": app_id}

                attach_url = f"{account}/numbers/{quote(target, safe='')}/application"
                resp = await client.post(attach_url, json={"application_id": app_id}, headers=self._headers)
                if resp.status_code == 404 and created is None:
                    # Recreate only if the stored Application is what's missing, not the number.
                    app = await client.get(f"{account}/Application/{app_id}/", headers=self._headers)
                    if app.status_code == 404:
                        app_id, error = await create_app()
                        if not app_id:
                            return InboundSyncResult(ok=False, message=error)
                        created = {"inbound_application_id": app_id}
                        resp = await client.post(attach_url, json={"application_id": app_id}, headers=self._headers)
                if resp.status_code not in (200, 201, 204):
                    if resp.status_code == 404:
                        message = f"Vobiz can't find {target} in this account"
                    else:
                        reason = _vobiz_error(resp)
                        message = f"Vobiz refused to attach {target} (HTTP {resp.status_code}: {reason})"
                        if reason == "access denied":
                            # Seen live on a free-trial number.
                            message += ". Vobiz trial numbers can't be attached to an application; use a paid number"
                    return InboundSyncResult(ok=False, message=message, credentials_update=created)

                if created is None and refresh_app:
                    resp = await client.post(f"{account}/Application/{app_id}/", json=app_fields, headers=self._headers)
                    if resp.status_code not in (200, 202):
                        return InboundSyncResult(
                            ok=False,
                            message=f"Vobiz rejected updating the application's URLs (HTTP {resp.status_code}: {_vobiz_error(resp)})",
                        )
        except httpx.HTTPError as exc:
            return InboundSyncResult(ok=False, message=f"couldn't reach Vobiz: {exc}", credentials_update=created)
        return InboundSyncResult(ok=True, credentials_update=created)

    async def refresh_inbound(self, urls: InboundUrls) -> InboundSyncResult | None:
        app_id = self._credentials.get("inbound_application_id")
        if not app_id:
            return None
        app_fields = {
            "answer_url": urls.answer_url, "answer_method": "POST",
            "hangup_url": urls.hangup_url, "hangup_method": "POST",
        }
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{_BASE_URL}/v1/Account/{self._auth_id}/Application/{app_id}/", json=app_fields, headers=self._headers,
                )
        except httpx.HTTPError as exc:
            return InboundSyncResult(ok=False, message=f"couldn't reach Vobiz: {exc}")
        if resp.status_code not in (200, 202):
            return InboundSyncResult(
                ok=False,
                message=f"Vobiz rejected updating the application's URLs (HTTP {resp.status_code}: {_vobiz_error(resp)})",
            )
        return InboundSyncResult(ok=True)

    async def discard_inbound_resources(self, credentials_update: dict[str, Any]) -> None:
        app_id = credentials_update.get("inbound_application_id")
        if not app_id:
            return
        url = f"{_BASE_URL}/v1/Account/{self._auth_id}/Application/{app_id}/"
        try:
            async with self._client() as client:
                resp = await client.delete(url, headers=self._headers)
            if resp.status_code not in (200, 204, 404):
                log.warning("vobiz: couldn't delete unused application %s (HTTP %s)", app_id, resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("vobiz: couldn't delete unused application %s: %s", app_id, exc)

    async def detach_inbound(self, number: str) -> InboundSyncResult:
        target = _e164(number)
        url = f"{_BASE_URL}/v1/Account/{self._auth_id}/numbers/{quote(target, safe='')}/application"
        try:
            async with self._client() as client:
                resp = await client.delete(url, headers=self._headers)
        except httpx.HTTPError as exc:
            return InboundSyncResult(ok=False, message=f"couldn't reach Vobiz: {exc}")
        if resp.status_code in (200, 204, 404):
            return InboundSyncResult(ok=True)
        return InboundSyncResult(ok=False, message=f"Vobiz rejected detaching {target} (HTTP {resp.status_code})")

    async def send_sms(self, *, from_number: str, to_number: str, text: str) -> str:
        """Assumes the Message API mirrors the Call API (unverified live)."""
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
