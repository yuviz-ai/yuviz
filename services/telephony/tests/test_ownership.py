from __future__ import annotations

import uuid

import httpx
import pytest

import services.config.cache as config_cache
from services.telephony import ownership as ownership_module
from services.telephony.accounts import accounts
from services.telephony.ownership import OwnershipError, resolve_outbound_identity


class _RedisDouble:
    def __init__(self, routes: dict[str, tuple[str, str]] | None = None):
        self.routes = routes or {}

    async def resolve(self, did: str):
        return self.routes.get(did)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        return _Response(self._status_code)


async def _async_headers():
    return {"Authorization": "Bearer x"}


@pytest.fixture(autouse=True)
def _reset_agent_memo(monkeypatch):
    accounts._agent_memo = {}
    monkeypatch.setattr(config_cache, "get_json", _async_cache_miss)
    monkeypatch.setattr(accounts, "auth_headers", _async_headers)
    yield
    accounts._agent_memo = {}


async def _async_cache_miss(key):
    return None


@pytest.mark.asyncio
async def test_own_did_and_agent_returns_identity(monkeypatch):
    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _RedisDouble({"1555": ("tenant-a", "x")}).resolve)
    accounts.remember_agent("tenant-a", "sales")

    identity = await resolve_outbound_identity(
        tenant_id=uuid.uuid4(), tenant_slug="tenant-a", requested_agent_slug="sales", from_number="1555",
    )
    assert identity.tenant_slug == "tenant-a"
    assert identity.agent_slug == "sales"
    assert identity.from_number == "1555"


@pytest.mark.asyncio
async def test_foreign_tenant_did_raises(monkeypatch):
    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _RedisDouble({"1555": ("tenant-b", "x")}).resolve)

    with pytest.raises(OwnershipError):
        await resolve_outbound_identity(
            tenant_id=uuid.uuid4(), tenant_slug="tenant-a", requested_agent_slug=None, from_number="1555",
        )


@pytest.mark.asyncio
async def test_foreign_tenant_agent_raises(monkeypatch):
    """Memo has the agent only under tenant-b; the Redis cache and Config
    Service repair fetch (both mocked to miss/404) are the only other paths
    that could admit it — neither does, so tenant-a's request is refused."""
    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _RedisDouble({"1555": ("tenant-a", "x")}).resolve)
    accounts.remember_agent("tenant-b", "sales")
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=None: _FakeClient(404))

    with pytest.raises(OwnershipError):
        await resolve_outbound_identity(
            tenant_id=uuid.uuid4(), tenant_slug="tenant-a", requested_agent_slug="sales", from_number="1555",
        )


@pytest.mark.asyncio
async def test_unknown_agent_memo_miss_redis_miss_config_404_raises(monkeypatch):
    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _RedisDouble({"1555": ("tenant-a", "x")}).resolve)
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=None: _FakeClient(404))

    with pytest.raises(OwnershipError):
        await resolve_outbound_identity(
            tenant_id=uuid.uuid4(), tenant_slug="tenant-a", requested_agent_slug="unknown-agent", from_number="1555",
        )


@pytest.mark.asyncio
async def test_unknown_agent_redis_hit_admits(monkeypatch):
    monkeypatch.setattr(ownership_module.did_route, "resolve_did_route", _RedisDouble({"1555": ("tenant-a", "x")}).resolve)

    async def _cache_hit(key):
        return {"exists": True}

    monkeypatch.setattr(config_cache, "get_json", _cache_hit)

    identity = await resolve_outbound_identity(
        tenant_id=uuid.uuid4(), tenant_slug="tenant-a", requested_agent_slug="sales", from_number="1555",
    )
    assert identity.agent_slug == "sales"
