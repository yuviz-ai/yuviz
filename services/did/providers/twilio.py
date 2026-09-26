"""
TwilioProvider — IDidProvider backed by Twilio's REST API.

Added 2026-07-26. search_available_numbers() is LIVE-VERIFIED (2026-07-26)
against a real Twilio account (Live credentials, not Test credentials —
Test credentials 403 on this endpoint with code 20008, "Resource not
accessible with Test Account Credentials", a real gotcha hit while
verifying this) — request shape, response envelope key
("available_phone_numbers"), and the capabilities casing quirk below were
all confirmed byte-for-byte against the real API with zero code changes
needed.

purchase_number()/release_number() remain UNVERIFIED — not yet tested
against a real account (a real purchase costs real money, deliberately
deferred). Treat their shapes as a strong prior (Twilio's 2010-04-01 API
has been stable and near-universally documented for over a decade) but
not a confirmation, same discipline as Plivo. Live-verify them the same
way before trusting a real purchase flow.

Confirmed/assumed API shape:
  - Base URL: https://api.twilio.com/2010-04-01/Accounts/{AccountSid}
  - Auth: HTTP Basic (AccountSid, AuthToken) — must be the account's LIVE
    credentials (console dashboard), not the Test Credentials shown
    alongside them — same as Plivo's auth mechanics, different credential
    pair (auth_id here holds AccountSid).
  - Search (LIVE-VERIFIED): GET /AvailablePhoneNumbers/{IsoCountryCode}/Local.json?AreaCode=415&PageSize=10
             Response envelope key is "available_phone_numbers"; each
             entry's "capabilities" dict uses Twilio's own inconsistent
             casing — {"voice": bool, "SMS": bool, "MMS": bool} — voice is
             lowercase, SMS/MMS are uppercase — confirmed live, not a typo.
  - Buy (UNVERIFIED): POST /IncomingPhoneNumbers.json  (form-encoded: PhoneNumber=+E164)
             Response includes "sid" (a real per-number resource id,
             "PNxxxxxxxx...") — unlike Plivo, which has no separate id and
             uses the bare number. This sid is what carrier_number_sid
             holds and what release() needs.
  - Release (UNVERIFIED): DELETE /IncomingPhoneNumbers/{Sid}.json
  - Twilio's available-numbers search does not return a monthly price in
    this endpoint (pricing lives under a separate /Pricing/v2 API this
    provider doesn't call) — monthly_price is left None rather than
    guessed. Confirmed live: the real response has no price field either.
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

        # Not every country sells every Twilio number type: "Local" 404s
        # outright (not an empty list) for countries that only offer Mobile
        # (e.g. India) — confirmed live, not a guess. Try Local first (it's
        # what most countries, including the US, actually have) and fall
        # back to Mobile only on that specific 404, so a real error on
        # either attempt still surfaces instead of being swallowed.
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
                    monthly_price=None,  # not returned by this endpoint — see module docstring
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
