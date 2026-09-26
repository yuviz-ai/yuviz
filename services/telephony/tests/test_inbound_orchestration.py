from __future__ import annotations

import ast
from pathlib import Path

import pytest
from starlette.requests import Request

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import orchestrator
from services.telephony.accounts import Account, accounts


def _make_request(*, path: str = "/fake/voice/cfg-a", headers: dict | None = None, query: str = "") -> Request:
    scope = {
        "type": "http", "method": "POST", "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": query.encode(),
        "server": ("test", 80), "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.fixture(autouse=True)
def _reset_state():
    orchestrator._per_account._buckets = {}
    orchestrator._per_did._buckets = {}
    orchestrator.callctx._pending = {}
    accounts._accounts = {
        ("fake", "cfg-a"): Account(
            provider="fake", account_ref="cfg-a", tenant_id="t-1", tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=FakeProvider({}),
        ),
    }
    accounts._loaded = True
    yield
    accounts._accounts = {}
    accounts._loaded = False


class _RedisDouble:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[str] = []

    async def resolve(self, did: str):
        self.calls.append(did)
        return self.routes.get(did)


def _wire_redis(monkeypatch, double: _RedisDouble) -> None:
    monkeypatch.setattr(orchestrator.did_route, "resolve_did_route", double.resolve)


@pytest.mark.asyncio
async def test_valid_signature_produces_server_minted_stream_url_and_one_context(monkeypatch):
    _wire_redis(monkeypatch, _RedisDouble({}))
    query = "call_id=call-1&from=1000&to=2000"
    resp = await orchestrator.handle_inbound_webhook(
        provider_name="fake", account_ref="cfg-a",
        request=_make_request(headers={"x-fake-signature": "valid"}, query=query),
    )
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "/fake/stream/" in body
    token = body.rsplit("/fake/stream/", 1)[1]
    assert token != "call-1"  # not the vendor's own call id
    assert orchestrator.callctx.pending_count == 1
    assert orchestrator.callctx.claim(token) is not None


@pytest.mark.asyncio
async def test_invalid_signature_is_403_empty_context_did_route_never_called(monkeypatch):
    double = _RedisDouble({})
    _wire_redis(monkeypatch, double)

    resp = await orchestrator.handle_inbound_webhook(
        provider_name="fake", account_ref="cfg-a",
        request=_make_request(headers={"x-fake-signature": "wrong"}),
    )
    assert resp.status_code == 403
    assert orchestrator.callctx.pending_count == 0
    assert double.calls == []


@pytest.mark.asyncio
async def test_unknown_provider_is_404_with_zero_ratelimit_increments():
    resp = await orchestrator.handle_inbound_webhook(
        provider_name="does-not-exist", account_ref="cfg-a", request=_make_request(),
    )
    assert resp.status_code == 404
    assert orchestrator._per_account._buckets == {}


@pytest.mark.asyncio
async def test_foreign_did_normalized_call_returns_403(monkeypatch):
    _wire_redis(monkeypatch, _RedisDouble({"2000": ("tenant-b", "sales")}))
    query = "call_id=call-1&from=1000&to=2000"
    resp = await orchestrator.handle_inbound_webhook(
        provider_name="fake", account_ref="cfg-a",
        request=_make_request(headers={"x-fake-signature": "valid"}, query=query),
    )
    assert resp.status_code == 403
    assert orchestrator.callctx.pending_count == 0


def test_provider_adapters_do_not_import_did_route_or_callctx():
    for module_path in ("libs/telephony_sdk/providers/vobiz.py", "libs/telephony_sdk/providers/cloudonix.py"):
        source = Path(module_path).read_text()
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(alias.name for alias in node.names)
        assert not any("did_route" in n for n in names), module_path
        assert not any("callctx" in n or "CallContextStore" in n or "HandoffStore" in n for n in names), module_path
