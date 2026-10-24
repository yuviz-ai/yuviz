from __future__ import annotations

from libs.telephony_sdk.exceptions import TelephonyProviderError, TelephonyRateLimited
from libs.telephony_sdk.interface import ITelephonyProvider


class _Unflagged(ITelephonyProvider):
    PROVIDER_NAME = "unflagged"

    @classmethod
    def required_credential_fields(cls):
        return []

    @classmethod
    def validate_credentials(cls, credentials):
        return None

    async def initiate_call(self, **kwargs):
        return "id"

    async def hangup_call(self, call_id):
        return None

    async def get_call_status(self, call_id):
        return {}

    def verify_webhook_signature(self, url, headers):
        return False

    def normalize_inbound_webhook(self, **kwargs):
        raise NotImplementedError


def test_rate_limited_is_a_provider_error_carrying_retry_after():
    exc = TelephonyRateLimited("slow down", retry_after=7)
    assert isinstance(exc, TelephonyProviderError)
    assert exc.retry_after == 7
    assert TelephonyRateLimited("x").retry_after is None


def test_unflagged_provider_reads_both_flags_false():
    assert _Unflagged.supports_campaign_outbound is False
    assert _Unflagged.signature_covers_fields is False
