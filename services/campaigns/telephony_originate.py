"""Places an outbound call via services.telephony's POST /{provider}/call
(HTTP, not ESL) — replaces vobiz_originate.py now that Vobiz/Cloudonix are
served by the unified telephony service. Every request carries
`Authorization: Bearer <service-account JWT>`, obtained with the same
login/401-retry-once helper services/vobiz/app.py:62-88 and
services/cloudonix/accounts.py:60-90 already share; a 401 is retried once
with a fresh token and then raises (not a transient condition worth the
backoff loop)."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)

TELEPHONY_SERVICE_URL = os.environ.get("TELEPHONY_SERVICE_URL", "http://localhost:8750").rstrip("/")
CONFIG_SERVICE_URL = os.environ.get("CONFIG_SERVICE_URL", "http://localhost:8000").rstrip("/")
_SERVICE_EMAIL = os.environ.get("CONFIG_SERVICE_EMAIL", "conversation-service@internal.yuviz.ai")
_SERVICE_PASSWORD = os.environ.get("CONFIG_SERVICE_PASSWORD", "")

_RETRY_BACKOFFS_S = (0.5, 1.0, 2.0)

_jwt_token: str | None = None


class TelephonyOriginateError(Exception):
    """Raised only if the call couldn't be placed — never for no-answer."""


class TelephonyOriginatePending(Exception):
    """Raised on a 202 — the vendor accepted but hasn't confirmed yet.
    Carries the idempotency_key so the caller can poll it later instead of
    treating this attempt as failed."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"telephony call pending, idempotency_key={idempotency_key}")
        self.idempotency_key = idempotency_key


async def _login() -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{CONFIG_SERVICE_URL}/auth/login",
            json={"email": _SERVICE_EMAIL, "password": _SERVICE_PASSWORD},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _post_with_auth(path: str, json_body: dict) -> httpx.Response:
    global _jwt_token
    if _jwt_token is None:
        _jwt_token = await _login()

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{TELEPHONY_SERVICE_URL}{path}", json=json_body,
            headers={"Authorization": f"Bearer {_jwt_token}"},
        )
        if resp.status_code == 401:
            _jwt_token = await _login()
            resp = await client.post(
                f"{TELEPHONY_SERVICE_URL}{path}", json=json_body,
                headers={"Authorization": f"Bearer {_jwt_token}"},
            )
    return resp


async def originate_call(
    *, provider: str, phone_number: str, caller_id: str,
    tenant_slug: str, agent_slug: str, idempotency_key: str,
) -> str:
    """Returns the vendor's call id. Retries the HTTP hop up to 3 times
    with 0.5/1/2s backoff on httpx.HTTPError or a 5xx, reusing the SAME
    idempotency_key every attempt (AC20) — the telephony service's own
    idempotency claim is what makes a retried POST safe, not a fresh key.
    A 202 raises TelephonyOriginatePending immediately (not a retry
    condition)."""
    body = {
        "tenant_slug": tenant_slug, "agent_slug": agent_slug,
        "to": phone_number, "from": caller_id, "idempotency_key": idempotency_key,
    }
    last_exc: Exception | None = None
    for attempt, backoff in enumerate((*_RETRY_BACKOFFS_S, None)):
        try:
            resp = await _post_with_auth(f"/{provider}/call", body)
        except httpx.HTTPError as exc:
            last_exc = exc
            if backoff is None:
                break
            await asyncio.sleep(backoff)
            continue

        if resp.status_code == 202:
            raise TelephonyOriginatePending(idempotency_key)
        if resp.status_code >= 500:
            last_exc = TelephonyOriginateError(f"telephony service returned {resp.status_code}: {resp.text}")
            if backoff is None:
                break
            await asyncio.sleep(backoff)
            continue
        if resp.status_code >= 400:
            raise TelephonyOriginateError(f"telephony service returned {resp.status_code}: {resp.text}")

        data = resp.json()
        if not data.get("ok"):
            raise TelephonyOriginateError(f"telephony place_call failed: {data.get('error')}")
        call_id = data.get("call_uuid")
        if not call_id:
            raise TelephonyOriginateError(f"telephony place_call response missing call_uuid: {data}")
        log.info(
            "telephony_originate: accepted phone_number=%s caller_id=%s call_id=%s",
            phone_number, caller_id, call_id,
        )
        return call_id

    raise TelephonyOriginateError(f"cannot reach telephony service at {TELEPHONY_SERVICE_URL}: {last_exc}")


async def poll_idempotency(*, provider: str, tenant_slug: str, idempotency_key: str) -> str | None:
    """Returns the vendor call id once the pending attempt resolves to
    'done', None while still pending/unfinalized (caller ticks again
    later), raises on a resolved 'failed' outcome."""
    global _jwt_token
    if _jwt_token is None:
        _jwt_token = await _login()

    path = f"/{provider}/call/idempotency/{idempotency_key}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{TELEPHONY_SERVICE_URL}{path}", params={"tenant_slug": tenant_slug},
            headers={"Authorization": f"Bearer {_jwt_token}"},
        )
        if resp.status_code == 401:
            _jwt_token = await _login()
            resp = await client.get(
                f"{TELEPHONY_SERVICE_URL}{path}", params={"tenant_slug": tenant_slug},
                headers={"Authorization": f"Bearer {_jwt_token}"},
            )

    if resp.status_code == 404:
        raise TelephonyOriginateError(f"idempotency key {idempotency_key!r} expired or unknown")
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "pending":
        return None
    if data.get("ok") is False:
        raise TelephonyOriginateError(f"telephony call resolved failed: {data.get('error')}")
    return data.get("call_uuid")
