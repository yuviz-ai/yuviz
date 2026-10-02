"""PlivoProvider — IDidProvider backed by Plivo's REST API (HTTP Basic auth).

UNVERIFIED: built from docs, never tested against a real account.
Plivo has no per-number resource id, so carrier_number_sid is the number itself.
"""

from __future__ import annotations

import logging

import httpx

from .interface import AvailableNumber, DidProviderError, PurchasedNumber

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.plivo.com"
_TYPE_MAP = {"US": "local"}  # assumed default; unverified for non-US countries


class PlivoProvider:
    def __init__(
        self,
        auth_id:    str,
        auth_token: str,
        base_url:   str = _DEFAULT_BASE_URL,
        timeout_s:  float = 15.0,
    ) -> None:
        self._auth_id = auth_id
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/v1/Account/{auth_id}",
            auth=(auth_id, auth_token),
            timeout=timeout_s,
        )
        log.info("PlivoProvider auth_id=%s (UNVERIFIED provider — see module docstring)", auth_id)

    async def search_available_numbers(
        self, country: str, area_code: str | None = None, limit: int = 10,
    ) -> list[AvailableNumber]:
        params: dict[str, str | int] = {
            "country_iso": country,
            "type": _TYPE_MAP.get(country.upper(), "local"),
            "limit": limit,
        }
        if area_code:
            params["pattern"] = area_code

        try:
            resp = await self._client.get("/PhoneNumber/", params=params)
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Plivo unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(f"Plivo /PhoneNumber/ search returned {resp.status_code}: {resp.text}")

        data = resp.json()
        return [
            AvailableNumber(
                phone_number=_to_e164(obj["number"]),
                region=obj.get("region"),
                monthly_price=obj.get("monthly_rental_rate"),
                capabilities=_capabilities_from(obj),
            )
            for obj in data.get("objects", [])
        ]

    async def purchase_number(self, phone_number: str) -> PurchasedNumber:
        plivo_number = phone_number.lstrip("+")
        try:
            resp = await self._client.post(f"/PhoneNumber/{plivo_number}/", json={})
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Plivo unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(f"Plivo purchase of {phone_number} returned {resp.status_code}: {resp.text}")

        return PurchasedNumber(phone_number=phone_number, carrier_number_sid=plivo_number)

    async def release_number(self, carrier_number_sid: str) -> None:
        plivo_number = carrier_number_sid.lstrip("+")
        try:
            resp = await self._client.delete(f"/Number/{plivo_number}/")
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Plivo unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(f"Plivo release of {carrier_number_sid} returned {resp.status_code}: {resp.text}")

    async def aclose(self) -> None:
        await self._client.aclose()


def _to_e164(plivo_number: str) -> str:
    """Plivo numbers (assumed) lack a leading '+'; normalize to E.164."""
    return plivo_number if plivo_number.startswith("+") else f"+{plivo_number}"


def _capabilities_from(obj: dict) -> tuple[str, ...]:
    caps = []
    if obj.get("voice_enabled"):
        caps.append("voice")
    if obj.get("sms_enabled"):
        caps.append("sms")
    if obj.get("mms_enabled"):
        caps.append("mms")
    return tuple(caps)
