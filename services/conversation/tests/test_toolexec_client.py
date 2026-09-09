"""
ToolExecClient tests — httpx.MockTransport, no real network, no cost.
Mirrors libs/knowledge_sdk/tests/test_http_repository.py's shape.
"""

from __future__ import annotations

import httpx

from services.conversation.tools.providers.toolexec.client import ToolExecClient


def _client(execute_handler, login_calls: list, execute_calls: list) -> ToolExecClient:
    def auth_handler(request: httpx.Request) -> httpx.Response:
        login_calls.append(request)
        return httpx.Response(200, json={"access_token": f"token-{len(login_calls)}"})

    def wrapped_execute_handler(request: httpx.Request) -> httpx.Response:
        execute_calls.append(request)
        return execute_handler(request)

    return ToolExecClient(
        base_url="http://toolexec-test",
        auth_base_url="http://config-test",
        service_email="conversation-service@internal.yuviz.ai",
        service_password="service-password",
        transport=httpx.MockTransport(wrapped_execute_handler),
        auth_transport=httpx.MockTransport(auth_handler),
    )


async def test_execute_chain_logs_in_lazily_on_first_call():
    login_calls: list = []
    execute_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token-1"
        return httpx.Response(200, json={"chain_status": "success", "data": {}})

    client = _client(handler, login_calls, execute_calls)
    result = await client.execute_chain({"api_name": "lookup_order", "chain_budget_ms": 20000})

    assert result == {"chain_status": "success", "data": {}}
    assert len(login_calls) == 1
    assert len(execute_calls) == 1
    await client.close()


async def test_execute_chain_reauthenticates_exactly_once_on_401():
    login_calls: list = []
    execute_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        # First call ever made carries the token from the lazy login above
        # (token-1); a 401 on it must trigger exactly one re-login
        # (token-2), never a retry loop.
        if request.headers["Authorization"] == "Bearer token-1":
            return httpx.Response(401)
        assert request.headers["Authorization"] == "Bearer token-2"
        return httpx.Response(200, json={"chain_status": "success", "data": {"order_id": "o1"}})

    client = _client(handler, login_calls, execute_calls)
    result = await client.execute_chain({"api_name": "lookup_order", "chain_budget_ms": 20000})

    assert result == {"chain_status": "success", "data": {"order_id": "o1"}}
    assert len(login_calls) == 2  # exactly one re-auth, not zero and not a retry loop
    assert len(execute_calls) == 2
    await client.close()


async def test_execute_chain_timeout_derived_from_chain_budget_not_the_fixed_constant():
    """BLOCKING 3: the server clamps to TOOLEXEC_MAX_CHAIN_BUDGET_MS
    (20-30s) and the UI ships a default timeout_ms of 20000, but the
    client hard-coded a 10s httpx timeout — any chain over 10s raised
    ReadTimeout client-side even though the server would have completed
    it (and any side effect it fired) within its own budget. The
    per-request timeout must scale with the request's own
    chain_budget_ms, not stay pinned to the constant used for /auth/login."""
    login_calls: list = []
    execute_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chain_status": "success", "data": {}})

    client = _client(handler, login_calls, execute_calls)
    await client.execute_chain({"api_name": "lookup_order", "chain_budget_ms": 25000})

    assert len(execute_calls) == 1
    timeout = execute_calls[0].extensions["timeout"]["read"]
    assert timeout > 10.0  # would have failed under the old fixed 10s constant
    assert timeout == 25.0 + 5.0  # chain_budget_ms/1000 + the round-trip margin
    await client.close()


async def test_execute_chain_raises_when_still_401_after_the_one_reauth():
    login_calls: list = []
    execute_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = _client(handler, login_calls, execute_calls)
    try:
        await client.execute_chain({"api_name": "lookup_order", "chain_budget_ms": 20000})
        assert False, "expected raise_for_status to raise"
    except httpx.HTTPStatusError:
        pass

    # Still exactly one re-auth attempt — a second 401 is not retried again.
    assert len(login_calls) == 2
    assert len(execute_calls) == 2
    await client.close()
