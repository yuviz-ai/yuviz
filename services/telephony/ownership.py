"""resolve_outbound_identity() — before any vendor call, verify the caller-id DID (and agent, for calls)
belong to the authenticated tenant."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from libs.telephony_sdk import did_route

from .accounts import CONFIG_SERVICE_URL, accounts

log = logging.getLogger("telephony.ownership")

_AGENT_FETCH_TIMEOUT_S = 2.0


class OwnershipError(Exception):
    """Raised for a caller-id DID or agent that does not belong to the
    authenticated tenant — the route handler turns this into a 403."""


@dataclass(frozen=True)
class OutboundIdentity:
    tenant_id: Any  # uuid.UUID
    tenant_slug: str
    agent_slug: str | None  # server-resolved; None for SMS
    from_number: str  # server-validated caller-id DID


async def resolve_outbound_identity(
    *, tenant_id: Any, tenant_slug: str, requested_agent_slug: str | None, from_number: str,
) -> OutboundIdentity:
    route = await did_route.resolve_did_route(from_number)
    if route is None or route[0] != tenant_slug:
        raise OwnershipError(f"caller-id {from_number!r} is not owned by tenant {tenant_slug!r}")

    if requested_agent_slug is not None:
        await _assert_agent_owned(tenant_slug, requested_agent_slug)

    return OutboundIdentity(
        tenant_id=tenant_id, tenant_slug=tenant_slug,
        agent_slug=requested_agent_slug, from_number=from_number,
    )


async def _assert_agent_owned(tenant_slug: str, agent_slug: str) -> None:
    """Memo (prewarmed) -> Redis agent cache -> one bounded Config Service fetch, memoized."""
    if accounts.agent_known(tenant_slug, agent_slug):
        return

    from services.config import cache as config_cache

    cached = await config_cache.get_json(f"agent:{tenant_slug}:{agent_slug}")
    if cached is not None:
        accounts.remember_agent(tenant_slug, agent_slug)
        return

    try:
        headers = await accounts.auth_headers()
        async with httpx.AsyncClient(timeout=_AGENT_FETCH_TIMEOUT_S) as client:
            resp = await client.get(
                f"{CONFIG_SERVICE_URL}/tenants/{tenant_slug}/agents/{agent_slug}",
                headers=headers,
            )
    except httpx.HTTPError:
        log.warning("telephony: agent ownership repair fetch failed tenant=%s agent=%s", tenant_slug, agent_slug)
        raise OwnershipError(f"agent {agent_slug!r} is not owned by tenant {tenant_slug!r}") from None

    if resp.status_code != 200:
        raise OwnershipError(f"agent {agent_slug!r} is not owned by tenant {tenant_slug!r}")

    accounts.remember_agent(tenant_slug, agent_slug)
