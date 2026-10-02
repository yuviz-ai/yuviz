"""TwilioProvider — IDidProvider backed by Twilio's REST API.

Auth is HTTP Basic with LIVE credentials (Test credentials 403 on search, code 20008).
Search is live-verified; purchase/release are UNVERIFIED. carrier_number_sid is Twilio's PN... sid.
"""

from __future__ import annotations

import logging

import httpx

from .interface import AvailableNumber, DidProviderError, PurchasedNumber

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.twilio.com"


class TwilioProvider:
    def __init__(
        self,
        account_sid: str,
        auth_token:  str,
        base_url:    str = _DEFAULT_BASE_URL,
        timeout_s:   float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/2010-04-01/Accounts/{account_sid}",
            auth=(account_sid, auth_token),
            timeout=timeout_s,
        )
        log.info("TwilioProvider account_sid=%s (not yet live-verified — see module docstring)", account_sid)

    async def search_available_numbers(
        self, country: str, area_code: str | None = None, limit: int = 10,
    ) -> list[AvailableNumber]:
        params: dict[str, str | int] = {"PageSize": limit}
        if area_code:
            params["AreaCode"] = area_code

        # "Local" 404s for Mobile-only countries (e.g. India); fall back to Mobile only on 404.
        last_error: tuple[int, str, str] | None = None
        for number_type in ("Local", "Mobile"):
            try:
                resp = await self._client.get(f"/AvailablePhoneNumbers/{country.upper()}/{number_type}.json", params=params)
            except httpx.HTTPError as exc:
                raise DidProviderError(f"Twilio unreachable: {exc}") from exc
            if resp.status_code == 404:
                last_error = (resp.status_code, number_type, resp.text)
                continue
            if resp.status_code >= 400:
                raise DidProviderError(
                    f"Twilio /AvailablePhoneNumbers/{country}/{number_type}.json search returned "
                    f"{resp.status_code}: {resp.text}"
                )
            data = resp.json()
            return [
                AvailableNumber(
                    phone_number=obj["phone_number"],
                    region=obj.get("region") or obj.get("locality"),
                    monthly_price=None,  # not returned by this endpoint
                    capabilities=_capabilities_from(obj),
                )
                for obj in data.get("available_phone_numbers", [])
            ]

        assert last_error is not None
        status, number_type, text = last_error
        raise DidProviderError(
            f"Twilio has no Local or Mobile numbers available for country={country!r} "
            f"(last attempt /AvailablePhoneNumbers/{country}/{number_type}.json returned {status}: {text})"
        )

    async def purchase_number(self, phone_number: str) -> PurchasedNumber:
        try:
            resp = await self._client.post("/IncomingPhoneNumbers.json", data={"PhoneNumber": phone_number})
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Twilio unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(f"Twilio purchase of {phone_number} returned {resp.status_code}: {resp.text}")

        data = resp.json()
        return PurchasedNumber(phone_number=data.get("phone_number", phone_number), carrier_number_sid=data["sid"])

    async def release_number(self, carrier_number_sid: str) -> None:
        try:
            resp = await self._client.delete(f"/IncomingPhoneNumbers/{carrier_number_sid}.json")
        except httpx.HTTPError as exc:
            raise DidProviderError(f"Twilio unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DidProviderError(
                f"Twilio release of {carrier_number_sid} returned {resp.status_code}: {resp.text}"
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def _capabilities_from(obj: dict) -> tuple[str, ...]:
    caps_obj = obj.get("capabilities") or {}
    caps = []
    if caps_obj.get("voice"):
        caps.append("voice")
    if caps_obj.get("SMS"):
        caps.append("sms")
    if caps_obj.get("MMS"):
        caps.append("mms")
    return tuple(caps)
