"""CampaignWorker — in-process asyncio pacing loop that dials 'running' campaigns."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from libs.telephony_sdk import providers as _telephony_providers  # noqa: F401 — registers every built-in provider
from libs.telephony_sdk.registry import TelephonyProviderRegistry
from libs.tenancy import platform_conn

from libs.telephony_sdk.exceptions import TelephonyRateLimited

from . import (
    attempts, campaign_contacts, campaigns, capacity, db, dnc, outcome_events, readiness, telephony_originate,
)
from .billing import BillingAdmission
from .dial_request import DialRequestClient, InvalidDialField, build_intent, is_e164
from .dnd_registry import AllowAllRegistry, IDndRegistry

log = logging.getLogger(__name__)

_TICK_INTERVAL_S = 2.0
_REGISTRY_TIMEOUT_S = 2.0
_RATE_LIMIT_BACKOFF_S = 30
_RATE_LIMIT_MAX_S = 300
_SCHEDULE_RECHECK_S = 60


def _parse_hhmm(value: str) -> dtime:
    hour, _, minute = value.partition(":")
    return dtime(int(hour), int(minute))


def _within_calling_hours(campaign: dict) -> bool:
    """A missing bound means unrestricted; windows may wrap midnight (22:00-06:00)."""
    start, end = campaign.get("calling_hours_start"), campaign.get("calling_hours_end")
    if not start or not end:
        return True
    now = datetime.now(ZoneInfo(campaign.get("calling_hours_timezone") or "UTC")).time()
    start_t, end_t = _parse_hhmm(start), _parse_hhmm(end)
    if start_t <= end_t:
        return start_t <= now <= end_t
    return now >= start_t or now <= end_t


class CampaignWorker:
    def __init__(
        self, *, dial_client: DialRequestClient, redis_client: redis.Redis, billing: BillingAdmission,
        egress_secret: bytes, registry: IDndRegistry | None = None,
    ) -> None:
        if billing.enabled:
            raise NotImplementedError("billing admission is not wired into the dial path yet")
        self._dial = dial_client
        self._billing = billing
        self._redis = redis_client
        self._egress_secret = egress_secret
        self._registry = registry or AllowAllRegistry()
        self._consumer = f"campaigns-{uuid.uuid4().hex[:8]}"
        self._groups_ready = False
        self._stopped = False
        self._task: asyncio.Task | None = None
        self._last_attempt_at: dict[str, float] = {}     # campaign_id -> monotonic time
        self._schedule_checked_at: dict[str, float] = {}  # scheduled campaign_id -> monotonic time of last check
        self._sending: set[uuid.UUID] = set()             # attempt ids with a send in progress in this process

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("CampaignWorker: tick failed")
            await asyncio.sleep(_TICK_INTERVAL_S)

    async def _tick(self) -> None:
        if not self._groups_ready:
            await outcome_events.ensure_groups(self._redis)
            self._groups_ready = True
        await self._drain_events()
        await self._reconcile()
        pool = await db.get_pool()
        # Loop stays outside the connection so no transaction spans the tick.
        await self._start_due_scheduled()
        async with platform_conn(pool, reason="campaign-worker-scan") as conn:
            running = await conn.fetch("SELECT * FROM campaigns WHERE status = 'running' AND deleted_at IS NULL")
        for row in running:
            await self._dial_next(dict(row))

    async def _start_due_scheduled(self) -> None:
        """A scheduled campaign starts only if the same readiness check that Start uses passes now; otherwise it waits."""
        pool = await db.get_pool()
        async with platform_conn(pool, reason="campaign-worker-scan") as conn:
            due = await conn.fetch(
                "SELECT * FROM campaigns WHERE status = 'scheduled' AND scheduled_start_at <= now() AND deleted_at IS NULL"
            )
        for row in due:
            campaign = dict(row)
            key = str(campaign["id"])
            now = time.monotonic()
            if now - self._schedule_checked_at.get(key, -_SCHEDULE_RECHECK_S) < _SCHEDULE_RECHECK_S:
                continue
            self._schedule_checked_at[key] = now
            try:
                result = await readiness.check(
                    campaign, tenant_id=campaign["tenant_id"], dial_client=self._dial, billing=self._billing,
                )
            except Exception:
                log.exception("CampaignWorker: readiness check failed for scheduled campaign=%s", key)
                continue
            if not result.ready:
                blocks = [c.message for c in result.checks if c.level == "block"]
                log.warning("CampaignWorker: scheduled campaign=%s waits, not ready: %s", key, "; ".join(blocks))
                continue
            async with platform_conn(pool, reason="campaign-dial-state") as conn:
                await conn.execute(
                    "UPDATE campaigns SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now() "
                    "WHERE id = $1 AND status = 'scheduled'",
                    campaign["id"],
                )
            log.info("CampaignWorker: scheduled campaign=%s started", key)

    async def _drain_events(self) -> None:
        await outcome_events.drain(self._redis, self._consumer)

    # ── reconcile ────────────────────────────────────────────────────────

    async def _reconcile(self) -> None:
        for row in await attempts.list_open():
            if row["id"] in self._sending:
                continue
            try:
                if row["outcome"] == "answered":
                    await self._reconcile_answered(row)
                elif row["call_session_id"] is not None and row["has_call"]:
                    await self._apply(row, "answered" if row["call_ended_at"] is None else "ended")
                elif row["path"] == "sip":
                    await self._reconcile_sip(row)
                else:
                    await self._reconcile_cpaas(row)
            except Exception:
                log.exception("CampaignWorker: reconcile failed attempt=%s", row["id"])

    async def _apply(self, row: dict[str, Any], outcome: str, **kwargs: Any) -> None:
        await attempts.apply_outcome(
            tenant_id=row["tenant_id"], key=row["idempotency_key"], provider_call_id=None, outcome=outcome,
            call_session_id=row["call_session_id"], **kwargs,
        )

    async def _reconcile_answered(self, row: dict[str, Any]) -> None:
        if row["call_ended_at"] is not None:
            await self._apply(row, "ended")
        elif row["end_overdue"]:
            await self._apply(row, "ended", failure_reason="end_not_observed")

    async def _fail_at_deadline(self, row: dict[str, Any]) -> None:
        await attempts.apply_outcome(
            tenant_id=row["tenant_id"], key=row["idempotency_key"], provider_call_id=None, outcome="failed",
            call_session_id=None, closed_by_deadline=True, failure_reason="deadline",
        )

    async def _reconcile_sip(self, row: dict[str, Any]) -> None:
        key = row["idempotency_key"]
        claimed = await self._redis.exists(f"campaign:claim:{key}")
        if row["past_deadline"]:
            if not claimed:
                await self._fail_at_deadline(row)
            return
        if row["state"] != "pending" or claimed or not row["resend_age"]:
            return
        if await self._redis.exists(f"campaign:intent:{key}") and await attempts.reserve_send(row["id"], path="sip") == "ok":
            await self._send_sip(row, first=False)

    async def _reconcile_cpaas(self, row: dict[str, Any]) -> None:
        if row["state"] == "pending" and row["send_due"] and row["resend_age"] and row["send_count"] < attempts.MAX_SENDS:
            try:
                result = await telephony_originate.reconcile_call(
                    provider=row["provider"], tenant_slug=row["tenant_slug"], idempotency_key=row["idempotency_key"],
                    to=row["callee"], caller_id=row["caller_id"], created_after=row["created_at"],
                )
            except telephony_originate.TelephonyOriginateError:
                log.warning("CampaignWorker: reconcile_call failed attempt=%s", row["id"])
                return
            if result["outcome"] == "placed":
                await attempts.mark_sent(row["id"], provider_call_id=result["provider_call_id"])
                return
            if result["outcome"] == "not_placed":
                if await attempts.reserve_send(row["id"], path="cpaas") == "ok":
                    await self._send_cpaas(row)
                return
        if not row["past_deadline"]:
            return
        if row["provider_call_id"] is not None:
            try:
                status = await telephony_originate.call_status(
                    provider=row["provider"], tenant_slug=row["tenant_slug"], call_id=row["provider_call_id"],
                )
            except telephony_originate.TelephonyOriginateError:
                log.warning("CampaignWorker: call_status failed attempt=%s, leaving it open", row["id"])
                return
            if status["status"] in ("live", "answered"):
                return
            if status["status"] in outcome_events._TERMINAL_API_STATUS:
                await attempts.apply_outcome(
                    tenant_id=row["tenant_id"], key=row["idempotency_key"], provider_call_id=row["provider_call_id"],
                    outcome=status["status"], call_session_id=None,
                )
                return
        await self._fail_at_deadline(row)

    # ── dial ─────────────────────────────────────────────────────────────

    async def _pause(self, campaign_id: str, why: str) -> None:
        log.warning("CampaignWorker: campaign=%s %s — pausing it; fix it and resume", campaign_id, why)
        await campaigns.set_status(campaign_id, "paused", platform_scoped=True)

    async def _dial_next(self, campaign: dict[str, Any]) -> None:
        campaign_id = str(campaign["id"])

        if not campaign.get("caller_id"):
            log.warning("CampaignWorker: campaign=%s has no caller_id configured, skipping", campaign_id)
            return

        contact = await campaign_contacts.peek_next_due(campaign["id"])
        if contact is None:
            await campaigns.set_wait_reason(campaign["id"], None)
            if await campaigns.is_finished(campaign["id"]):
                await campaigns.set_status(campaign["id"], "completed", platform_scoped=True)
                log.info("CampaignWorker: campaign=%s completed (no contacts remain)", campaign_id)
            return

        if not _within_calling_hours(campaign):
            await campaigns.set_wait_reason(campaign["id"], "calling_hours")
            return

        now = time.monotonic()
        last = self._last_attempt_at.get(campaign_id)
        if last is not None and now - last < campaign["pacing_seconds"]:
            await campaigns.set_wait_reason(campaign["id"], "pacing")
            return

        route = await campaigns.resolve_outbound_route(
            campaign["tenant_id"], campaign["agent_id"], campaign["caller_id"], platform_scoped=True,
        )
        # Gate on ownership and agent before the provider branch: provider is None for an unowned DID too.
        if not route["caller_id_owned"] or not route["agent_ok"]:
            await self._pause(campaign_id, "caller ID is not owned by its tenant or the agent is unavailable")
            return
        provider = route["provider"]
        if provider is None or provider == "native":
            path, cps_scope = "sip", capacity.SIP_TRUNK_SCOPE
        elif provider in TelephonyProviderRegistry.all():
            path, cps_scope = "cpaas", capacity.provider_scope(route["telephony_config_id"])
        else:
            await self._pause(campaign_id, f"caller ID is bound to unregistered provider {provider!r}")
            return

        if not is_e164(campaign["caller_id"]):
            await self._pause(campaign_id, "caller_id is not an E.164 number")
            return
        contact_id = str(contact["id"])
        if not is_e164(contact["phone_number"]):
            log.warning("CampaignWorker: contact=%s phone_number is not E.164 — failing it without retry", contact_id)
            await campaign_contacts.mark_contact_status(contact["id"], "failed", platform_scoped=True)
            return

        # Catches numbers DNC-listed after upload; costs no pacing slot.
        if await dnc.is_blocked(campaign["tenant_id"], contact["phone_number"], platform_scoped=True):
            log.info(
                "CampaignWorker: contact=%s phone=%s is on the DNC list — blocking",
                contact_id, attempts._mask_callee(contact["phone_number"]),
            )
            await campaign_contacts.mark_contact_status(contact["id"], "blocked", platform_scoped=True)
            return
        try:
            verdict = await asyncio.wait_for(
                self._registry.check(contact["phone_number"], campaign.get("purpose"), campaign["tenant_id"]),
                _REGISTRY_TIMEOUT_S,
            )
        except Exception:
            log.exception("CampaignWorker: do-not-disturb registry check failed campaign=%s", campaign_id)
            await campaigns.set_wait_reason(campaign["id"], "registry")
            return
        if verdict == "deny":
            await campaign_contacts.mark_contact_status(contact["id"], "blocked", platform_scoped=True)
            return

        claim = await campaign_contacts.claim_next_due(
            campaign, contact_id=contact["id"], path=path, provider=provider, cps_scope=cps_scope,
        )
        if isinstance(claim, campaign_contacts.Waiting):
            return
        self._last_attempt_at[campaign_id] = now
        attempt = {**claim.attempt, "tenant_slug": route["tenant_slug"], "agent_slug": route["agent_slug"]}
        if path == "sip":
            await self._send_sip(attempt, first=True)
        else:
            await self._send_cpaas(attempt)

    # ── send ─────────────────────────────────────────────────────────────

    async def _send_sip(self, attempt: dict[str, Any], *, first: bool) -> None:
        self._sending.add(attempt["id"])
        try:
            intent = build_intent(
                key=attempt["idempotency_key"], tenant_id=str(attempt["tenant_id"]),
                campaign_id=str(attempt["campaign_id"]), contact_id=str(attempt["contact_id"]),
                call_uuid=attempt["call_session_id"], callee=attempt["callee"], caller_id=attempt["caller_id"],
                tenant_slug=attempt["tenant_slug"], agent_slug=attempt["agent_slug"], max_call_s=attempt["max_call_s"],
                egress_secret=self._egress_secret,
            )
            result = await self._dial.send(intent, first=first)
            if result in ("accepted", "duplicate"):
                await attempts.mark_sent(attempt["id"], provider_call_id=None)
            elif result in ("rejected", "no_node"):
                await self._fail_send(attempt, "dial_rejected" if result == "rejected" else "no_sip_node")
        except InvalidDialField:
            log.exception("CampaignWorker: attempt=%s has a field that cannot be dialled", attempt["id"])
            await self._fail_send(attempt, "invalid_dial_field")
        finally:
            self._sending.discard(attempt["id"])

    async def _send_cpaas(self, attempt: dict[str, Any]) -> None:
        self._sending.add(attempt["id"])
        try:
            call_id = await telephony_originate.originate_call(
                provider=attempt["provider"], phone_number=attempt["callee"], caller_id=attempt["caller_id"],
                tenant_slug=attempt["tenant_slug"], agent_slug=attempt["agent_slug"],
                idempotency_key=attempt["idempotency_key"],
            )
        except TelephonyRateLimited as exc:
            delay = min(_RATE_LIMIT_MAX_S, exc.retry_after or _RATE_LIMIT_BACKOFF_S * 2 ** (attempt["send_count"] - 1))
            await attempts.defer_send(attempt["id"], delay_s=delay)
        except (telephony_originate.TelephonyOriginatePending, telephony_originate.TelephonyOriginateError):
            # The vendor may already have dialled: leave it pending for reconcile_call to decide.
            log.warning("CampaignWorker: send not confirmed attempt=%s", attempt["id"])
        else:
            await attempts.mark_sent(attempt["id"], provider_call_id=call_id)
        finally:
            self._sending.discard(attempt["id"])

    async def _fail_send(self, attempt: dict[str, Any], reason: str) -> None:
        log.error("CampaignWorker: nothing was sent attempt=%s reason=%s", attempt["id"], reason)
        await attempts.apply_outcome(
            tenant_id=attempt["tenant_id"], key=attempt["idempotency_key"], provider_call_id=None, outcome="failed",
            call_session_id=None, failure_reason=reason,
        )
