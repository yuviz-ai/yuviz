"""Place outbound calls via services.telephony's POST /{provider}/call (service-account JWT)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import httpx

from libs.telephony_sdk.exceptions import TelephonyRateLimited

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
    """Raised on a 202 (vendor not yet confirmed); carries the idempotency_key to poll."""

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
    """Return the vendor call id; retries transport errors/5xx with the SAME idempotency_key."""
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
        if resp.status_code == 429:
            raise TelephonyRateLimited("telephony provider rate limit", retry_after=resp.json().get("retry_after"))
        if resp.status_code >= 400:
            raise TelephonyOriginateError(f"telephony service returned {resp.status_code}: {resp.text}")

        data = resp.json()
        if not data.get("ok"):
            raise TelephonyOriginateError(f"telephony place_call failed: {data.get('error')}")
        call_id = data.get("call_uuid")
        if not call_id:
            raise TelephonyOriginateError(f"telephony place_call response missing call_uuid: {data}")
        log.info(
            "telephony_originate: accepted call_id=%s", call_id,
        )
        return call_id

    raise TelephonyOriginateError(f"cannot reach telephony service at {TELEPHONY_SERVICE_URL}: {last_exc}")


async def _get_with_auth(path: str, params: dict, *, timeout: float) -> httpx.Response:
    global _jwt_token
    if _jwt_token is None:
        _jwt_token = await _login()

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{TELEPHONY_SERVICE_URL}{path}", params=params, headers={"Authorization": f"Bearer {_jwt_token}"},
        )
        if resp.status_code == 401:
            _jwt_token = await _login()
            resp = await client.get(
                f"{TELEPHONY_SERVICE_URL}{path}", params=params, headers={"Authorization": f"Bearer {_jwt_token}"},
            )
    return resp


async def poll_idempotency(*, provider: str, tenant_slug: str, idempotency_key: str) -> str | None:
    """Vendor call id once resolved, None while pending; raises on a failed outcome."""
    resp = await _get_with_auth(
        f"/{provider}/call/idempotency/{idempotency_key}", {"tenant_slug": tenant_slug}, timeout=10.0,
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


async def outbound_readiness(*, provider: str, tenant_slug: str) -> dict:
    """The telephony service's {"ready": bool, "reason": str | None} for this tenant's provider account."""
    try:
        resp = await _get_with_auth(f"/{provider}/outbound-readiness", {"tenant_slug": tenant_slug}, timeout=10.0)
    except httpx.HTTPError as exc:
        raise TelephonyOriginateError(f"cannot reach telephony service at {TELEPHONY_SERVICE_URL}: {exc}") from exc
    if resp.status_code >= 400:
        raise TelephonyOriginateError(f"telephony service returned {resp.status_code}: {resp.text}")
    return resp.json()


async def reconcile_call(
    *, provider: str, tenant_slug: str, idempotency_key: str, to: str, caller_id: str, created_after: datetime,
) -> dict:
    """{"outcome": "placed" | "not_placed" | "unknown", "provider_call_id": str | None} for a possibly-sent key."""
    body = {
        "tenant_slug": tenant_slug, "idempotency_key": idempotency_key,
        "to": to, "from": caller_id, "created_after": created_after.isoformat(),
    }
    try:
        resp = await _post_with_auth(f"/{provider}/call/reconcile", body)
    except httpx.HTTPError as exc:
        raise TelephonyOriginateError(f"cannot reach telephony service at {TELEPHONY_SERVICE_URL}: {exc}") from exc
    if resp.status_code >= 400:
        raise TelephonyOriginateError(f"telephony service returned {resp.status_code}: {resp.text}")
    return resp.json()


async def call_status(*, provider: str, tenant_slug: str, call_id: str) -> dict:
    """The provider API's view of a call: {"status": live|ended|busy|no_answer|declined|failed|unknown, "to", "from"}."""
    try:
        resp = await _get_with_auth(f"/{provider}/call/{call_id}/status", {"tenant_slug": tenant_slug}, timeout=10.0)
    except httpx.HTTPError as exc:
        raise TelephonyOriginateError(f"cannot reach telephony service at {TELEPHONY_SERVICE_URL}: {exc}") from exc
    if resp.status_code >= 400:
        raise TelephonyOriginateError(f"telephony service returned {resp.status_code}: {resp.text}")
    return resp.json()
