"""PlivoProvider tests via MockTransport; mocks encode the assumed (unverified) API shape."""

from __future__ import annotations

import httpx
import pytest

from services.did.providers.interface import DidProviderError
from services.did.providers.plivo import PlivoProvider

_SEARCH_RESPONSE = {
    "api_id": "abc123",
    "meta": {"limit": 20, "offset": 0, "total_count": 1},
    "objects": [
        {
            "number": "14155551234", "prefix": "415", "region": "California, United States",
            "type": "fixed", "monthly_rental_rate": "0.80", "voice_enabled": True, "sms_enabled": True,
        },
    ],
}


def _make_provider(handler) -> PlivoProvider:
    provider = PlivoProvider(auth_id="MAtest", auth_token="test-token")
    provider._client = httpx.AsyncClient(
        base_url="https://api.plivo.com/v1/Account/MAtest", transport=httpx.MockTransport(handler),
    )
    return provider


async def test_search_available_numbers_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/PhoneNumber/")
        assert request.url.params["country_iso"] == "US"
        assert request.url.params["pattern"] == "415"
        return httpx.Response(200, json=_SEARCH_RESPONSE)

    provider = _make_provider(handler)
    results = await provider.search_available_numbers("US", area_code="415")

    assert len(results) == 1
    assert results[0].phone_number == "+14155551234"
    assert results[0].region == "California, United States"
    assert results[0].monthly_price == "0.80"
    assert set(results[0].capabilities) == {"voice", "sms"}


async def test_search_empty_result_is_empty_list_not_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"api_id": "x", "meta": {}, "objects": []})

    provider = _make_provider(handler)
    assert await provider.search_available_numbers("US") == []


async def test_search_4xx_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid credentials"})

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.search_available_numbers("US")


async def test_purchase_number_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/PhoneNumber/14155551234/")
        return httpx.Response(202, json={
            "api_id": "abc123", "message": "created", "numbers": [{"number": "14155551234", "status": "success"}],
        })

    provider = _make_provider(handler)
    result = await provider.purchase_number("+14155551234")

    assert result.phone_number == "+14155551234"
    assert result.carrier_number_sid == "14155551234"


async def test_purchase_number_4xx_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "number no longer available"})

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.purchase_number("+14155551234")


async def test_release_number_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path.endswith("/Number/14155551234/")
        return httpx.Response(204)

    provider = _make_provider(handler)
    await provider.release_number("14155551234")  # must not raise


async def test_release_number_4xx_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "number not found"})

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.release_number("14155551234")


async def test_network_failure_raises_did_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _make_provider(handler)
    with pytest.raises(DidProviderError):
        await provider.search_available_numbers("US")
