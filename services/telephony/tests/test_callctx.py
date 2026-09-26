from __future__ import annotations

import time

import pytest

from services.telephony.callctx import CallContextStore, CallRoute, HandoffCapacityError


def _route(*, account_ref: str = "cfg-a", provider_call_id: str = "call-123") -> CallRoute:
    return CallRoute(
        tenant_slug="acme", agent_slug="ivr", caller_did="1", called_did="2",
        provider="vobiz", account_ref=account_ref, provider_call_id=provider_call_id,
        issued_at=time.monotonic(),
    )


def test_token_not_derivable_from_provider_call_id():
    store = CallContextStore()
    route = _route(provider_call_id="call-123")
    token = store.issue(route)
    assert token != route.provider_call_id
    assert route.provider_call_id not in token


def test_connecting_with_provider_call_id_in_path_is_refused():
    store = CallContextStore()
    route = _route(provider_call_id="call-123")
    store.issue(route)
    assert store.claim("call-123") is None


def test_claim_is_single_use():
    store = CallContextStore()
    token = store.issue(_route())
    assert store.claim(token) is not None
    assert store.claim(token) is None


def test_token_issued_under_account_a_resolves_only_to_account_a():
    store = CallContextStore()
    token_a = store.issue(_route(account_ref="account-a"))
    token_b = store.issue(_route(account_ref="account-b"))

    route_a = store.claim(token_a)
    assert route_a is not None
    assert route_a.account_ref == "account-a"

    route_b = store.claim(token_b)
    assert route_b is not None
    assert route_b.account_ref == "account-b"

    # No token exists that resolves to the other account's route.
    assert store.claim(token_a) is None
    assert store.claim(token_b) is None


def test_claim_past_ttl_returns_none():
    store = CallContextStore()
    route = _route()
    stale = CallRoute(
        tenant_slug=route.tenant_slug, agent_slug=route.agent_slug,
        caller_did=route.caller_did, called_did=route.called_did,
        provider=route.provider, account_ref=route.account_ref,
        provider_call_id=route.provider_call_id,
        issued_at=time.monotonic() - CallContextStore._TTL_S - 1,
    )
    token = "tok"
    store._pending[token] = stale
    assert store.claim(token) is None


def test_capacity_raises():
    store = CallContextStore()
    store._pending = {f"tok-{i}": _route() for i in range(CallContextStore._MAX_PENDING)}
    with pytest.raises(HandoffCapacityError):
        store.issue(_route())
