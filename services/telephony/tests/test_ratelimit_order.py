"""Invalid-signature requests hit 429 once the limit is exhausted: rate limiting runs before signature checks."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import orchestrator
from services.telephony.accounts import Account, accounts


def _make_request(path: str = "/fake/voice/cfg-a", headers: dict | None = None) -> Request:
    scope = {
        "type": "http", "method": "POST", "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"",
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


@pytest.mark.asyncio
async def test_all_invalid_signature_requests_get_429_once_over_limit():
    limit = orchestrator._ACCOUNT_LIMIT
    last_status = None
    for _ in range(limit + 1):
        resp = await orchestrator.handle_inbound_webhook(
            provider_name="fake", account_ref="cfg-a", request=_make_request(headers={"x-fake-signature": "wrong"}),
        )
        last_status = resp.status_code
    assert last_status == 429
