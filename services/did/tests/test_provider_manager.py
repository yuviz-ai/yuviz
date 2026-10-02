"""DidProviderManager unit tests with fake factories; no DB, no network."""

from __future__ import annotations

import pytest

from services.did.provider_manager import _DEFAULT_REGISTRY, DidProviderManager


class _FakeSecretResolver:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    async def resolve(self, ref: str) -> str:
        return self._values.get(ref, "resolved-secret")


class _FakeProvider:
    def __init__(self, carrier: dict, auth_token: str | None) -> None:
        self.carrier = carrier
        self.auth_token = auth_token


async def _fake_factory(carrier: dict, auth_token: str | None):
    return _FakeProvider(carrier, auth_token)


def _carrier(**overrides) -> dict:
    base = {
        "id": "carrier-1", "provider": "plivo", "auth_id": "MAtest",
        "auth_token_ref": "env:PLIVO_AUTH_TOKEN",
    }
    base.update(overrides)
    return base


async def test_get_constructs_and_caches_by_carrier_id():
    manager = DidProviderManager(_FakeSecretResolver(), registry={"plivo": _fake_factory})
    carrier = _carrier()

    first = await manager.get(carrier)
    second = await manager.get(carrier)

    assert first is second
    assert manager.cached_ids() == frozenset({"carrier-1"})


async def test_get_resolves_auth_token_ref_once():
    resolver = _FakeSecretResolver({"env:PLIVO_AUTH_TOKEN": "real-token-value"})
    manager = DidProviderManager(resolver, registry={"plivo": _fake_factory})

    provider = await manager.get(_carrier())

    assert provider.auth_token == "real-token-value"


async def test_get_with_no_auth_token_ref_passes_none():
    manager = DidProviderManager(_FakeSecretResolver(), registry={"plivo": _fake_factory})
    carrier = _carrier(auth_token_ref=None)

    provider = await manager.get(carrier)

    assert provider.auth_token is None


async def test_get_unregistered_provider_raises_clear_error():
    manager = DidProviderManager(_FakeSecretResolver(), registry={})

    with pytest.raises(ValueError, match="no IDidProvider factory registered"):
        await manager.get(_carrier())


def test_default_registry_has_plivo_and_twilio():
    assert set(_DEFAULT_REGISTRY.keys()) == {"plivo", "twilio"}


async def test_different_carriers_get_different_instances():
    manager = DidProviderManager(_FakeSecretResolver(), registry={"plivo": _fake_factory})

    a = await manager.get(_carrier(id="carrier-a"))
    b = await manager.get(_carrier(id="carrier-b"))

    assert a is not b
    assert manager.cached_ids() == frozenset({"carrier-a", "carrier-b"})
