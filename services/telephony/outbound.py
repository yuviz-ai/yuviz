"""
place_call() / send_sms() — resolve ownership (already done by the caller,
via OutboundIdentity) -> claim -> vendor -> finalize, with the
timeout-reconciliation branch (AC13-17, AC19, findings #1-3).

Neither function takes a raw tenant_slug/agent_slug/from_number: only the
`OutboundIdentity` ownership.resolve_outbound_identity() returns, and it is
the sole source of tenant_id for every idempotency.* call inside them
(lesson 31/32).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from . import idempotency
from .accounts import Account, accounts
from .callctx import outbound_identities
from .ownership import OutboundIdentity

log = logging.getLogger("telephony.outbound")

OutboundResult = tuple[int, dict[str, Any]]


class OutboundUnavailable(Exception):
    """Raised when the tenant has no usable account for the requested
    provider — the route handler turns this into a 503."""


def _public_base_url() -> str:
    return os.environ["TELEPHONY_PUBLIC_BASE_URL"].rstrip("/")


def _resolve_account(provider: str, tenant_slug: str) -> Account:
    account = accounts.default_outbound_for(tenant_slug)
    if account is None or account.provider != provider:
        raise OutboundUnavailable(f"tenant {tenant_slug!r} has no default-outbound {provider!r} account")
    return account


async def place_call(
    *, provider: str, identity: OutboundIdentity, to_number: str, idempotency_key: str,
) -> OutboundResult:
    account = _resolve_account(provider, identity.tenant_slug)

    won = await idempotency.claim(provider, identity.tenant_id, idempotency_key)
    if not won:
        cached = await idempotency.await_outcome(provider, identity.tenant_id, idempotency_key)
        if cached is None:
            return 202, {"status": "pending", "idempotency_key": idempotency_key}
        return result_from_outcome(cached)

    # Remembered BEFORE dialling: the vendor's answer_url callback re-enters
    # this same process's inbound webhook handler (it is the identical
    # /{provider}/voice/{account_ref} route), which must find this outbound
    # leg's real agent_slug/tenant_slug waiting for it rather than resolving
    # a route via DID lookup against the callee's number (findings #2/#3).
    if identity.agent_slug is not None:
        outbound_identities.remember(
            provider, account.account_ref, idempotency_key,
            tenant_slug=identity.tenant_slug, agent_slug=identity.agent_slug,
        )

    try:
        call_id = await account.instance.initiate_call(
            from_number=identity.from_number, to_number=to_number,
            answer_url=f"{_public_base_url()}/{provider}/voice/{account.account_ref}?idem={idempotency_key}",
            hangup_url=f"{_public_base_url()}/{provider}/status/{account.account_ref}?idem={idempotency_key}&event=hangup",
            ring_url=f"{_public_base_url()}/{provider}/status/{account.account_ref}?idem={idempotency_key}&event=ring",
        )
        # Remember under the vendor's returned call_id immediately — Cloudonix's
        # fixed answer_url can't carry our ?idem= param, so this is the only way
        # to match the answer webhook if it fires before this function returns.
        if identity.agent_slug is not None:
            outbound_identities.remember(
                provider, account.account_ref, call_id,
                tenant_slug=identity.tenant_slug, agent_slug=identity.agent_slug,
            )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return await _reconcile_timeout(provider, identity, account, idempotency_key)
    except Exception as exc:
        outcome = {"state": "failed", "error": str(exc)}
        await idempotency.finalize(provider, identity.tenant_id, idempotency_key, outcome)
        return 200, {"ok": False, "error": str(exc)}

    outcome = {"state": "done", "call_uuid": call_id}
    await idempotency.finalize(provider, identity.tenant_id, idempotency_key, outcome)
    return 200, {"ok": True, "call_uuid": call_id}


async def send_sms(
    *, identity: OutboundIdentity, to_number: str, text: str, idempotency_key: str,
) -> OutboundResult:
    account = accounts.default_outbound_for(identity.tenant_slug)
    if account is None:
        raise OutboundUnavailable(f"tenant {identity.tenant_slug!r} has no default-outbound account")
    provider = account.provider

    won = await idempotency.claim(provider, identity.tenant_id, idempotency_key)
    if not won:
        cached = await idempotency.await_outcome(provider, identity.tenant_id, idempotency_key)
        if cached is None:
            return 202, {"status": "pending", "idempotency_key": idempotency_key}
        return result_from_outcome(cached, id_field="message_id")

    try:
        message_id = await account.instance.send_sms(
            from_number=identity.from_number, to_number=to_number, text=text,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return await _reconcile_timeout(provider, identity, account, idempotency_key, id_field="message_id")
    except Exception as exc:
        outcome = {"state": "failed", "error": str(exc)}
        await idempotency.finalize(provider, identity.tenant_id, idempotency_key, outcome)
        return 200, {"ok": False, "error": str(exc)}

    outcome = {"state": "done", "message_id": message_id}
    await idempotency.finalize(provider, identity.tenant_id, idempotency_key, outcome)
    return 200, {"ok": True, "message_id": message_id}


async def _reconcile_timeout(
    provider: str, identity: OutboundIdentity, account: Account, idempotency_key: str, *, id_field: str = "call_uuid",
) -> OutboundResult:
    observed = await idempotency.observed_call_id(provider, identity.tenant_id, idempotency_key)

    if id_field == "message_id":
        result = await account.instance.reconcile_message(reference=idempotency_key, observed_message_id=observed)
    else:
        result = await account.instance.reconcile_call(reference=idempotency_key, observed_call_id=observed)

    if result.outcome == "placed":
        outcome = {"state": "done", id_field: result.provider_call_id}
        await idempotency.finalize(provider, identity.tenant_id, idempotency_key, outcome)
        return 200, {"ok": True, id_field: result.provider_call_id}
    if result.outcome == "not_placed":
        outcome = {"state": "failed", "error": "not placed"}
        await idempotency.finalize(provider, identity.tenant_id, idempotency_key, outcome)
        return 200, {"ok": False}
    # indeterminate — leave the claim in_flight (no finalize), never a second dial.
    return 202, {"status": "pending", "idempotency_key": idempotency_key}


def result_from_outcome(outcome: dict[str, Any], *, id_field: str = "call_uuid") -> OutboundResult:
    if outcome.get("state") == "done":
        body: dict[str, Any] = {"ok": True}
        if id_field in outcome:
            body[id_field] = outcome[id_field]
        return 200, body
    return 200, {"ok": False, "error": outcome.get("error")}
