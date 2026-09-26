"""AC3-AC5 and the admission rule: resolve_did_route must distinguish a
miss from a hit, stay bounded on a slow/unreachable Redis, and recover
once Redis is fast again. Also covers CloudonixProvider's credential
validation and its always-False verify_webhook_signature (R2-3/enc:-only
guards live here rather than in a separate file per the task list)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import redis.asyncio as redis

from libs.config_sdk.secrets import encrypt_secret, generate_key
from libs.telephony_sdk import did_route
from libs.telephony_sdk.exceptions import TelephonyProviderError
from libs.telephony_sdk.providers.cloudonix import CloudonixProvider


@pytest.fixture(autouse=True)
def _secret_encryption_key(monkeypatch):
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", generate_key())


class _FakeClient:
    def __init__(self, *, value=None, error: Exception | None = None, delay: float = 0.0):
        self._value = value
        self._error = error
        self._delay = delay

    async def get(self, key: str):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._value


@pytest.fixture(autouse=True)
def _isolate_client(monkeypatch):
    monkeypatch.setattr(did_route, "_client", None)
    monkeypatch.setenv("DID_REDIS_TIMEOUT_MS", "50")


def _use(monkeypatch, client) -> None:
    monkeypatch.setattr(did_route, "_get_client", lambda: client)


async def test_resolve_did_route_hit(monkeypatch):
    _use(monkeypatch, _FakeClient(value=json.dumps({"tenant_slug": "acme", "agent_slug": "ivr"})))
    assert await did_route.resolve_did_route("15551234567") == ("acme", "ivr")


async def test_resolve_did_route_miss(monkeypatch):
    _use(monkeypatch, _FakeClient(value=None))
    assert await did_route.resolve_did_route("15551234567") is None


async def test_resolve_did_route_malformed_json(monkeypatch):
    _use(monkeypatch, _FakeClient(value="not json"))
    assert await did_route.resolve_did_route("15551234567") is None


async def test_resolve_did_route_malformed_missing_keys(monkeypatch):
    _use(monkeypatch, _FakeClient(value=json.dumps({"tenant_slug": "acme"})))
    assert await did_route.resolve_did_route("15551234567") is None


async def test_resolve_did_route_redis_error(monkeypatch):
    _use(monkeypatch, _FakeClient(error=redis.ConnectionError("down")))
    assert await did_route.resolve_did_route("15551234567") is None


async def test_resolve_did_route_timeout_bounded(monkeypatch):
    _use(monkeypatch, _FakeClient(delay=5.0))
    start = time.monotonic()
    result = await did_route.resolve_did_route("15551234567")
    elapsed = time.monotonic() - start
    assert result is None
    assert elapsed < 2 * did_route._timeout_s()


async def test_resolve_did_route_recovers_after_timeout(monkeypatch):
    _use(monkeypatch, _FakeClient(delay=5.0))
    assert await did_route.resolve_did_route("15551234567") is None

    _use(monkeypatch, _FakeClient(value=json.dumps({"tenant_slug": "acme", "agent_slug": "ivr"})))
    assert await did_route.resolve_did_route("15551234567") == ("acme", "ivr")


async def test_resolve_did_falls_back_to_default_on_every_none_case(monkeypatch):
    _use(monkeypatch, _FakeClient(value=None))
    assert await did_route.resolve_did("15551234567") == ("default", "default")


async def test_resolve_did_returns_real_route_on_hit(monkeypatch):
    _use(monkeypatch, _FakeClient(value=json.dumps({"tenant_slug": "acme", "agent_slug": "ivr"})))
    assert await did_route.resolve_did("15551234567") == ("acme", "ivr")


def test_verify_webhook_signature_always_false_even_for_valid_key():
    provider = CloudonixProvider({"domain": "a.cloudonix.io", "api_keys": [encrypt_secret("k")]})
    assert provider.verify_webhook_signature("https://example/cloudonix/voice/cfg-a", {}) is False


def test_validate_credentials_rejects_empty_api_keys():
    with pytest.raises(TelephonyProviderError):
        CloudonixProvider.validate_credentials({"domain": "a.cloudonix.io", "api_keys": []})


def test_validate_credentials_rejects_missing_domain():
    with pytest.raises(TelephonyProviderError):
        CloudonixProvider.validate_credentials({"api_keys": [encrypt_secret("k")]})


def test_validate_credentials_rejects_env_ref():
    with pytest.raises(TelephonyProviderError):
        CloudonixProvider.validate_credentials({"domain": "a.cloudonix.io", "api_keys": ["env:PATH"]})


def test_validate_credentials_rejects_k8s_ref():
    with pytest.raises(TelephonyProviderError):
        CloudonixProvider.validate_credentials({"domain": "a.cloudonix.io", "api_keys": ["k8s:/etc/passwd"]})


def test_validate_credentials_rejects_raw_key():
    with pytest.raises(TelephonyProviderError):
        CloudonixProvider.validate_credentials({"domain": "a.cloudonix.io", "api_keys": ["raw-secret-value"]})


def test_validate_credentials_accepts_enc_token():
    CloudonixProvider.validate_credentials({"domain": "a.cloudonix.io", "api_keys": [encrypt_secret("k")]})


def test_build_answer_response_uses_connect_stream_and_quoteattr():
    provider = CloudonixProvider({"domain": "a.cloudonix.io", "api_keys": [encrypt_secret("k")]})
    xml = provider.build_answer_response("wss://example/cloudonix/stream/tok&en")
    assert "<Connect><Stream url=" in xml
    assert "tok&amp;en" in xml
