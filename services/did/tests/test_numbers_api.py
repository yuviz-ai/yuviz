"""
Tests the actual HTTP layer (routing, request validation, status codes,
error mapping) in-process via httpx's ASGITransport — same convention as
services/config/tests/test_api.py. get_provider_manager is overridden with
a fake IDidProvider registry so this never needs real carrier credentials
(none exist yet — see provider_manager.py's docstring).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from services.did.app import app
from services.did.provider_manager import DidProviderManager
from services.did.providers.interface import AvailableNumber, DidProviderError, PurchasedNumber
from services.did.runtime import get_provider_manager


class _FakeSecretResolver:
    async def resolve(self, ref: str) -> str:
        return "fake-token"


class _FakeProvider:
    def __init__(self, search_results=None, purchase_raises=None, release_raises=None) -> None:
        self._search_results = search_results if search_results is not None else [
            AvailableNumber(phone_number="+14155550100", region="CA", monthly_price="$1.00", capabilities=("voice",)),
        ]
        self._purchase_raises = purchase_raises
        self._release_raises = release_raises
        self.released_sids: list[str] = []
        self.purchased_numbers: list[str] = []

    async def search_available_numbers(self, country, area_code=None, limit=10):
        return self._search_results

    async def purchase_number(self, phone_number):
        self.purchased_numbers.append(phone_number)
        if self._purchase_raises:
            raise self._purchase_raises
        return PurchasedNumber(phone_number=phone_number, carrier_number_sid="PN_fake_123")

    async def release_number(self, carrier_number_sid):
        if self._release_raises:
            raise self._release_raises
        self.released_sids.append(carrier_number_sid)


def _override_provider_manager(fake_provider: _FakeProvider):
    async def _factory(carrier, auth_token):
        return fake_provider

    manager = DidProviderManager(_FakeSecretResolver(), registry={"plivo": _factory})
    app.dependency_overrides[get_provider_manager] = lambda: manager
    return manager


@pytest.fixture
async def client(test_superadmin):
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {test_superadmin['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def foreign_client(foreign_tenant_admin):
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {foreign_tenant_admin['token']}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c
    app.dependency_overrides.clear()


async def test_search_available_numbers(client, test_tenant, test_carrier):
    _override_provider_manager(_FakeProvider())

    resp = await client.get(
        f"/tenants/{test_tenant['id']}/numbers/search",
        params={"carrier_id": test_carrier["id"], "country": "US", "area_code": "415"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == [{
        "phone_number": "+14155550100", "region": "CA", "monthly_price": "$1.00", "capabilities": ["voice"],
    }]


async def test_search_with_nonexistent_carrier_is_404(client, test_tenant):
    resp = await client.get(
        f"/tenants/{test_tenant['id']}/numbers/search",
        params={"carrier_id": "00000000-0000-0000-0000-000000000000", "country": "US"},
    )
    assert resp.status_code == 404


async def test_search_provider_error_is_502(client, test_tenant, test_carrier):
    fake = _FakeProvider()

    async def _search_raises(*a, **kw):
        raise DidProviderError("carrier unreachable")
    fake.search_available_numbers = _search_raises
    _override_provider_manager(fake)

    resp = await client.get(
        f"/tenants/{test_tenant['id']}/numbers/search",
        params={"carrier_id": test_carrier["id"], "country": "US"},
    )
    assert resp.status_code == 502


async def test_purchase_number_creates_purchased_numbers_row(client, test_tenant, test_carrier):
    _override_provider_manager(_FakeProvider())

    resp = await client.post(
        f"/tenants/{test_tenant['id']}/numbers/purchase",
        json={"carrier_id": str(test_carrier["id"]), "phone_number": "+14155550199"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["phone_number"] == "+14155550199"
    assert body["carrier_number_sid"] == "PN_fake_123"
    assert body["phone_number_id"] is None

    listed = await client.get(f"/tenants/{test_tenant['id']}/numbers")
    assert any(p["id"] == body["id"] for p in listed.json())


async def test_purchase_into_foreign_tenant_is_refused_before_any_carrier_call(
    client, foreign_client, test_tenant, test_carrier,
):
    """Design Q6 / test plan 4d: an admin of one tenant hitting
    `/tenants/{other}/numbers/purchase` must be refused, and refused
    before the billable carrier call — RLS cannot undo a purchase already
    placed with the carrier, so the check has to happen first."""
    fake = _FakeProvider()
    _override_provider_manager(fake)

    resp = await foreign_client.post(
        f"/tenants/{test_tenant['id']}/numbers/purchase",
        json={"carrier_id": str(test_carrier["id"]), "phone_number": "+14155550196"},
    )
    assert resp.status_code == 403
    assert fake.purchased_numbers == []

    listed = await client.get(f"/tenants/{test_tenant['id']}/numbers")
    assert all(p["phone_number"] != "+14155550196" for p in listed.json())


async def test_purchase_conflict_is_502(client, test_tenant, test_carrier):
    fake = _FakeProvider(purchase_raises=DidProviderError("number no longer available"))
    _override_provider_manager(fake)

    resp = await client.post(
        f"/tenants/{test_tenant['id']}/numbers/purchase",
        json={"carrier_id": str(test_carrier["id"]), "phone_number": "+14155550198"},
    )
    assert resp.status_code == 502


async def test_release_number_calls_provider_and_marks_released(client, test_tenant, test_carrier):
    fake = _FakeProvider()
    _override_provider_manager(fake)

    purchase_resp = await client.post(
        f"/tenants/{test_tenant['id']}/numbers/purchase",
        json={"carrier_id": str(test_carrier["id"]), "phone_number": "+14155550197"},
    )
    purchased_id = purchase_resp.json()["id"]

    release_resp = await client.post(f"/numbers/{purchased_id}/release")
    assert release_resp.status_code == 200
    assert release_resp.json()["released_at"] is not None
    assert fake.released_sids == ["PN_fake_123"]

    listed = await client.get(f"/tenants/{test_tenant['id']}/numbers")
    assert all(p["id"] != purchased_id for p in listed.json())


async def test_release_nonexistent_purchased_number_is_404(client, test_tenant, test_carrier):
    _override_provider_manager(_FakeProvider())
    resp = await client.post("/numbers/00000000-0000-0000-0000-000000000000/release")
    assert resp.status_code == 404
