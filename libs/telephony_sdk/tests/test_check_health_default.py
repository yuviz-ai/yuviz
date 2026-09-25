from __future__ import annotations

import pytest

from libs.telephony_sdk.interface import ITelephonyProvider, NormalizedInboundCall


class _NoHealthOverride(ITelephonyProvider):
    PROVIDER_NAME = "no-health-override"

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

    def build_answer_response(self, websocket_url):
        return websocket_url

    def normalize_inbound_webhook(self, *, url, headers, fields, account_tenant_slug):
        return NormalizedInboundCall(
            provider_call_id="id", from_number="", to_number="", known_tenant_slug=None, raw={},
        )


@pytest.mark.asyncio
async def test_default_check_health_returns_true():
    provider = _NoHealthOverride({})
    assert await provider.check_health() is True
