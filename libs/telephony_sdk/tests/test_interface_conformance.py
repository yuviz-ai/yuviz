from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_ENCRYPTION_KEY", "test-key-not-a-real-fernet-key")

from libs.telephony_sdk import providers  # noqa: F401 — registers every built-in provider
from libs.telephony_sdk.interface import (
    ISmsProvider,
    ITelephonyProvider,
    NormalizedInboundCall,
)
from libs.telephony_sdk.registry import SmsProviderRegistry, TelephonyProviderRegistry


def _provider_module_count() -> int:
    providers_dir = Path(providers.__file__).parent
    return sum(
        1 for f in providers_dir.glob("*.py")
        if f.name not in ("__init__.py",)
    )


def test_registry_size_matches_provider_module_count():
    # A provider added without conforming (e.g. missing an abstract method
    # override) would fail to import, so this enumeration itself fails
    # rather than silently omitting the new module (lesson 12, lesson 29).
    assert len(TelephonyProviderRegistry.all()) == _provider_module_count()


def test_every_registered_telephony_provider_instantiates():
    for name, provider_cls in TelephonyProviderRegistry.all().items():
        credentials = {f: "x" for f in provider_cls.required_credential_fields()}
        instance = provider_cls(credentials)
        assert isinstance(instance, ITelephonyProvider), name


def test_every_registered_sms_provider_instantiates():
    for name, provider_cls in SmsProviderRegistry.all().items():
        credentials = {f: "x" for f in provider_cls.required_credential_fields()}
        instance = provider_cls(credentials)
        assert isinstance(instance, ISmsProvider), name


def test_fake_hidden_from_visible_but_resolves_by_name():
    assert "fake" not in TelephonyProviderRegistry.visible()
    assert TelephonyProviderRegistry.get("fake") is not None
    assert "fake" not in SmsProviderRegistry.visible()
    assert SmsProviderRegistry.get("fake") is not None


def test_subclass_without_normalize_inbound_webhook_is_abstract():
    class _Incomplete(ITelephonyProvider):
        PROVIDER_NAME = "incomplete"

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

    with pytest.raises(TypeError, match="abstract"):
        _Incomplete({})


def test_subclass_implementing_normalize_inbound_webhook_instantiates_with_defaults():
    class _Minimal(ITelephonyProvider):
        PROVIDER_NAME = "minimal"

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
                provider_call_id="id", from_number="", to_number="",
                known_tenant_slug=None, raw={},
            )

    instance = _Minimal({})
    assert instance.sensitive_credential_fields() == []


@pytest.mark.asyncio
async def test_concrete_defaults():
    class _Minimal(ITelephonyProvider):
        PROVIDER_NAME = "minimal"

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
                provider_call_id="id", from_number="", to_number="",
                known_tenant_slug=None, raw={},
            )

    from libs.telephony_sdk.exceptions import TelephonyTransferUnsupported

    instance = _Minimal({})
    assert instance.sensitive_credential_fields() == []
    assert await instance.check_health() is True
    with pytest.raises(TelephonyTransferUnsupported):
        await instance.transfer_call(call_id="x", destination="y")
    result = await instance.reconcile_call(reference="r", observed_call_id=None)
    assert result.outcome == "indeterminate"
