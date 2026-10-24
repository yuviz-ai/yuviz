from __future__ import annotations

import httpx
import pytest

from libs.telephony_sdk.exceptions import TelephonyProviderError, TelephonyRateLimited
from libs.telephony_sdk.providers.cloudonix import CloudonixProvider
from libs.telephony_sdk.providers.fake import FakeProvider
from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider


def _provider(handler) -> VobizTelephonyProvider:
    p = VobizTelephonyProvider({"auth_id": "id", "auth_token": "tok"})
    p._http_transport = httpx.MockTransport(handler)
    return p


async def _dial(provider):
    return await provider.initiate_call(from_number="+14155550100", to_number="+14155550101", answer_url="https://x/a")


async def test_429_with_retry_after_raises_rate_limited():
    provider = _provider(lambda req: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(TelephonyRateLimited) as exc:
        await _dial(provider)
    assert exc.value.retry_after == 7


async def test_500_still_raises_the_generic_error():
    provider = _provider(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(TelephonyProviderError) as exc:
        await _dial(provider)
    assert not isinstance(exc.value, TelephonyRateLimited)


async def test_201_returns_call_id():
    provider = _provider(lambda req: httpx.Response(201, json={"call_uuid": "c-1"}))
    assert await _dial(provider) == "c-1"


def test_campaign_capability_flags():
    assert VobizTelephonyProvider.supports_campaign_outbound is True
    assert FakeProvider.supports_campaign_outbound is True
    assert CloudonixProvider.supports_campaign_outbound is False


@pytest.mark.parametrize("record, status", [
    ({"call_state": "ringing"}, "ringing"),
    ({"call_state": "in-progress", "answer_time": "2026-10-24 17:08:55"}, "in-progress"),
    ({"call_state": None, "hangup_cause": "USER_BUSY", "end_time": "t"}, "busy"),
    ({"call_state": None, "hangup_cause": "NO_ANSWER", "end_time": "t"}, "no-answer"),
    ({"call_state": None, "hangup_cause": "CALL_REJECTED", "end_time": "t"}, "rejected"),
    ({"call_state": None, "hangup_cause": "NORMAL_CLEARING", "answer_time": "t", "end_time": "t"}, "completed"),
    ({"call_state": None, "hangup_cause": "NETWORK_OUT_OF_ORDER", "end_time": "t"}, "failed"),
])
async def test_vobiz_call_status_is_reported_in_the_platforms_terms(record, status):
    body = {**record, "to_number": "918971188211", "from_number": "918065354620"}
    provider = _provider(lambda req: httpx.Response(200, json=body))
    result = await provider.get_call_status("c-1")
    assert (result["status"], result["to"], result["from"]) == (status, "918971188211", "918065354620")
