from __future__ import annotations

from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider


def test_vobiz_instantiates_without_abstract_error():
    provider = VobizTelephonyProvider({"auth_id": "id", "auth_token": "tok"})
    assert provider.sensitive_credential_fields() == ["auth_token"]


def test_normalize_inbound_webhook_parses_form_payload():
    provider = VobizTelephonyProvider({"auth_id": "id", "auth_token": "tok"})
    fields = {"CallUUID": "call-123", "To": "5551234567", "From": "5559876543"}
    call = provider.normalize_inbound_webhook(
        url="https://example.test/vobiz/answer", headers={}, fields=fields,
        account_tenant_slug="acme",
    )
    assert call.provider_call_id == "call-123"
    assert call.to_number == "5551234567"
    assert call.from_number == "5559876543"
    assert call.known_tenant_slug is None
