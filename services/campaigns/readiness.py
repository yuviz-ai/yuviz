"""Start/resume readiness: blocks that stop a campaign dialing, and warnings that do not.

Capacity limits never block: a full cap means wait, not "cannot dial"."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from libs.telephony_sdk import providers as _telephony_providers  # noqa: F401 — registers every built-in provider
from libs.telephony_sdk.registry import TelephonyProviderRegistry
from libs.tenancy import platform_conn

from . import campaigns, db, telephony_originate
from .billing import BILLING_TIMEOUT_S, BillingAdmission, CostEstimate, estimate_campaign
from .dial_request import DialRequestClient

log = logging.getLogger(__name__)

CheckId = Literal[
    "agent_unavailable", "caller_id_missing", "caller_id_not_owned",
    "no_pending_contacts", "timezone_invalid", "provider_not_ready", "provider_not_campaign_capable",
    "no_healthy_sip_node", "campaign_cap_above_tenant_cap",
]


@dataclass(frozen=True)
class Check:
    id: CheckId
    level: Literal["block", "warn"]
    field: str
    message: str
    count: int | None = None


@dataclass(frozen=True)
class Readiness:
    ready: bool
    checks: list[Check]
    estimate: CostEstimate | None


def _block(check_id: CheckId, field: str, message: str, count: int | None = None) -> Check:
    return Check(check_id, "block", field, message, count)


async def check(
    campaign: dict, *, tenant_id: UUID, dial_client: DialRequestClient, billing: BillingAdmission,
) -> Readiness:
    """`tenant_id` is the campaign's own tenant, already authorised by the caller."""
    checks: list[Check] = []
    pool = await db.get_pool()
    async with platform_conn(pool, reason="campaign-by-id") as conn:
        agent_ok = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM agents WHERE id = $1 AND tenant_id = $2 AND status = 'active' "
            "AND deleted_at IS NULL AND direction <> 'inbound')",
            campaign["agent_id"], tenant_id,
        )
        pending = await conn.fetchval(
            "SELECT count(*) FROM campaign_contacts cc JOIN campaigns c ON c.id = cc.campaign_id "
            "WHERE cc.campaign_id = $1 AND c.tenant_id = $2 AND cc.status = 'pending'",
            campaign["id"], tenant_id,
        )
        tenant_cap = await conn.fetchval(
            "SELECT max_outbound_concurrent_calls FROM tenants WHERE id = $1", tenant_id,
        )

    estimate = await _estimate(campaign, billing)

    if not agent_ok:
        checks.append(_block("agent_unavailable", "agent_id", "agent not found or not active"))
    if pending == 0:
        checks.append(_block("no_pending_contacts", "contacts", "the campaign has no contacts left to call", 0))
    try:
        ZoneInfo(campaign["calling_hours_timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        checks.append(_block("timezone_invalid", "calling_hours_timezone", "the calling-hours timezone is not valid"))

    if not campaign.get("caller_id"):
        checks.append(_block("caller_id_missing", "caller_id", "set an outbound caller ID on the campaign's agent"))
    else:
        checks += await _caller_id_checks(campaign, tenant_id, dial_client)

    if tenant_cap is not None and campaign["max_concurrent_calls"] > tenant_cap:
        checks.append(Check(
            "campaign_cap_above_tenant_cap", "warn", "max_concurrent_calls",
            f"this campaign allows {campaign['max_concurrent_calls']} concurrent calls but the tenant limit set by "
            f"the platform admin is {tenant_cap}; the lower number applies",
        ))
    return Readiness(ready=not any(c.level == "block" for c in checks), checks=checks, estimate=estimate)


async def _estimate(campaign: dict, billing: BillingAdmission) -> CostEstimate | None:
    """On its own connection, so a slow or failing estimate costs the readiness answer nothing."""
    try:
        pool = await db.get_pool()
        async with platform_conn(pool, reason="campaign-by-id") as conn:
            return await asyncio.wait_for(estimate_campaign(conn, billing, campaign), BILLING_TIMEOUT_S)
    except Exception:
        log.warning("cost estimate unavailable", exc_info=True)
        return None


async def _caller_id_checks(campaign: dict, tenant_id: UUID, dial_client: DialRequestClient) -> list[Check]:
    route = await campaigns.resolve_outbound_route(
        tenant_id, campaign["agent_id"], campaign["caller_id"], platform_scoped=True,
    )
    if not route["caller_id_owned"]:
        return [_block("caller_id_not_owned", "caller_id", "the caller ID is not a number owned by this account")]

    provider = route["provider"]
    if provider is None or provider == "native":
        return await _sip_checks(campaign, route, dial_client)
    provider_cls = TelephonyProviderRegistry.all().get(provider)
    if provider_cls is None or not provider_cls.supports_campaign_outbound:
        return [_block("provider_not_campaign_capable", "caller_id", f"{provider} cannot be used for campaigns")]
    try:
        status = await telephony_originate.outbound_readiness(provider=provider, tenant_slug=route["tenant_slug"])
    except telephony_originate.TelephonyOriginateError:
        return [_block("provider_not_ready", "caller_id", f"cannot reach the telephony service for {provider}")]
    if not status["ready"]:
        return [_block("provider_not_ready", "caller_id", f"{provider} is not ready: {status['reason']}")]
    return []


async def _sip_checks(campaign: dict, route: dict, dial_client: DialRequestClient) -> list[Check]:
    checks = []
    if not await dial_client.probe():
        checks.append(_block("no_healthy_sip_node", "caller_id", "no SIP node is available to place calls"))
    return checks
