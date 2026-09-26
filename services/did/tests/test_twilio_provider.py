"""
TwilioProvider tests — httpx.MockTransport, no real network.

*** These test the ASSUMED shape documented in providers/twilio.py's
module docstring, not a confirmed real API contract *** — unlike
test_cal_com_provider.py (which mirrors shapes actually captured live).
Confidence here is higher than test_plivo_provider.py's (Twilio's
2010-04-01 API is long-stable and near-universally documented), but it is
still a prior, not a confirmation. When real Twilio credentials exist and
this provider is live-verified, these tests should be checked against
whatever the real API actually returns and corrected if anything differs.
"""

from __future__ import annotations

import httpx
import pytest

from services.did.providers.interface import DidProviderError
from services.did.providers.twilio import TwilioProvider

_SEARCH_RESPONSE = {
    "available_phone_numbers": [
        {
            "friendly_name": "(415) 555-1234", "phone_number": "+14155551234",
            "locality": "San Francisco", "region": "CA",
            "capabilities": {"voice": True, "SMS": True, "MMS": False},
        },
    ],
    "uri": "/2010-04-01/Accounts/ACtest/AvailablePhoneNumbers/US/Local.json",
}


def _make_provider(handler) -> TwilioProvider:
    provider = TwilioProvider(account_sid="ACtest", auth_token="test-token")
    provider._client = httpx.AsyncClient(
        base_url="https://api.twilio.com/2010-04-01/Accounts/ACtest", transport=httpx.MockTransport(handler),
    )
    return provider


async def test_search_available_numbers_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/AvailablePhoneNumbers/US/Local.json")
        assert request.url.params["AreaCode"] == "415"
        return httpx.Response(200, json=_SEARCH_RESPONSE)

    provider = _make_provider(handler)
    results = await provider.search_available_numbers("US", area_code="415")

    assert len(results) == 1
    assert results[0].phone_number == "+14155551234"
    assert results[0].region == "CA"
    assert results[0].monthly_price is None
    assert set(results[0].capabilities) == {"voice", "sms"}


async def test_search_empty_result_is_empty_list_not_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available_phone_numbers": [], "uri": "x"})

    provider = _make_provider(handler)
    assert await provider.search_available_numbers("US") == []


async def test_search_falls_back_to_mobile_when_local_404s():
    # Confirmed live: countries that only sell Mobile numbers (e.g. India)
    # 404 outright on /Local.json rather than returning an empty list.
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/Local.json"):
            return httpx.Response(404, json={"code": 20404, "message": "not found"})
        assert request.url.path.endswith("/Mobile.json")
        return httpx.Response(200, json=_SEARCH_RESPONSE)

    provider = _make_provider(handler)
    results = await provider.search_available_numbers("IN")

    assert len(results) == 1
    assert calls == [
        "/2010-04-01/Accounts/ACtest/AvailablePhoneNumbers/IN/Local.json",
        "/2010-04-01/Accounts/ACtest/AvailablePhoneNumbers/IN/Mobile.json",
    ]


async def test_search_raises_when_both_local_and_mobile_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": 20404, "message": "not found"})

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.search_available_numbers("XX")


async def test_search_4xx_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.search_available_numbers("US")


async def test_purchase_number_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/IncomingPhoneNumbers.json")
        return httpx.Response(201, json={
            "sid": "PN1234567890abcdef1234567890abcdef", "phone_number": "+14155551234",
        })

    provider = _make_provider(handler)
    result = await provider.purchase_number("+14155551234")

    assert result.phone_number == "+14155551234"
    assert result.carrier_number_sid == "PN1234567890abcdef1234567890abcdef"


async def test_purchase_number_4xx_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 21422, "message": "not available"})

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.purchase_number("+14155551234")


async def test_release_number_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path.endswith("/IncomingPhoneNumbers/PN1234567890abcdef1234567890abcdef.json")
        return httpx.Response(204)

    provider = _make_provider(handler)
    await provider.release_number("PN1234567890abcdef1234567890abcdef")  # must not raise


async def test_release_number_4xx_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": 20404, "message": "not found"})

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.release_number("PN1234567890abcdef1234567890abcdef")


async def test_network_failure_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.search_available_numbers("US")
