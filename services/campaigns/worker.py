"""
CampaignWorker — the background pacing loop that actually places calls for
'running' campaigns. Runs as an asyncio background task inside the
FastAPI app's own process (see app.py's lifespan) rather than a separate
process like services/knowledge/'s ingestion worker — a deliberate v1
simplification given campaign call volume on a single dev machine is
nowhere near what would justify a separate scaled-out worker process;
revisit if real usage needs it.

*** UNVERIFIED END TO END, 2026-07-28 *** — see originate.py's module
docstring. This module's own logic (pacing, concurrency, claiming
contacts, retry-on-failure) is fully real and testable; what's unverified
is specifically whether the ESL commands it issues actually produce a
working AI phone call once a real trunk/DID exists.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from libs.tenancy import platform_conn

from . import campaign_contacts, campaigns, db, dnc, originate

log = logging.getLogger(__name__)

_TICK_INTERVAL_S = 2.0


def _parse_hhmm(value: str) -> dtime:
    hour, _, minute = value.partition(":")
    return dtime(int(hour), int(minute))


def _within_calling_hours(campaign: dict) -> bool:
    """None/None on either bound means unrestricted — preserves existing
    campaigns' behavior from before this guardrail existed. A window that
    wraps past midnight (e.g. 22:00-06:00) is supported by treating it as
    "outside [end, start)" instead of "inside [start, end]"."""
    start, end = campaign.get("calling_hours_start"), campaign.get("calling_hours_end")
    if not start or not end:
        return True
    now = datetime.now(ZoneInfo(campaign.get("calling_hours_timezone") or "UTC")).time()
    start_t, end_t = _parse_hhmm(start), _parse_hhmm(end)
    if start_t <= end_t:
        return start_t <= now <= end_t
    return now >= start_t or now <= end_t


class CampaignWorker:
    def __init__(self) -> None:
        self._stopped = False
        self._task: asyncio.Task | None = None
        self._last_attempt_at: dict[str, float] = {}     # campaign_id -> monotonic time
        self._in_flight: dict[str, int] = {}              # campaign_id -> count of 'calling' contacts
        # job_uuid -> (campaign_id, contact_id, max_attempts, attempt_count-at-dial-time) —
        # max_attempts/attempt_count captured here so _on_job_complete can
        # decide retry-vs-exhaust without an extra DB round trip.
        self._job_to_contact: dict[str, tuple[str, str, int, int]] = {}
        self._event_listener = originate.EslJobEventListener(self._on_job_complete)

    def start(self) -> None:
        self._event_listener.start()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._event_listener.stop()

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
        pool = await db.get_pool()
        # Cross-tenant scan, no request context — the loop itself stays
        # outside the connection so it never pins a transaction snapshot
        # for the tick's lifetime (lesson: a worker loop inside a
        # transaction holds locks for as long as the loop runs).
        async with platform_conn(pool, reason="campaign-worker-scan") as conn:
            running = await conn.fetch("SELECT * FROM campaigns WHERE status = 'running' AND deleted_at IS NULL")
        for row in running:
            await self._tick_campaign(dict(row))

    async def _tick_campaign(self, campaign: dict) -> None:
        campaign_id = str(campaign["id"])
        now = time.monotonic()

        last = self._last_attempt_at.get(campaign_id, 0.0)
        if now - last < campaign["pacing_seconds"]:
            return  # too soon since the last dial for this campaign

        in_flight = self._in_flight.get(campaign_id, 0)
        if in_flight >= campaign["max_concurrent_calls"]:
            return  # already at this campaign's own concurrency cap

        if not campaign.get("caller_id"):
            log.warning("CampaignWorker: campaign=%s has no caller_id configured, skipping", campaign_id)
            return

        if not _within_calling_hours(campaign):
            return  # outside the configured window — try again next tick, no pacing/attempt cost

        contact = await campaign_contacts.claim_next_pending(campaign_id)
        if contact is None:
            progress = await campaigns.get_progress(campaign_id)
            if progress["calling"] == 0:
                # No pending contacts left and nothing still in flight —
                # the campaign is genuinely done, not just paced-out.
                await campaigns.set_status(campaign_id, "completed")
                log.info("CampaignWorker: campaign=%s completed (no contacts remain)", campaign_id)
            return

        # DNC re-check, defense in depth: the primary enforcement point is
        # the upload endpoint (routers/campaigns.py), which never lets a
        # blocked number become a 'pending' row in the first place. This
        # catches a number added to the DNC list after upload, for a
        # campaign that's already running. Deliberately doesn't touch
        # pacing/in_flight — no real dial attempt happens, so it shouldn't
        # cost this campaign a pacing slot.
        if await dnc.is_blocked(campaign["tenant_id"], contact["phone_number"]):
            log.info("CampaignWorker: contact=%s phone=%s is on the DNC list — blocking", contact["id"], contact["phone_number"])
            await campaign_contacts.mark_contact_status(contact["id"], "blocked")
            return

        self._last_attempt_at[campaign_id] = now
        self._in_flight[campaign_id] = in_flight + 1
        max_attempts = campaign["max_attempts"]
        attempt_count = contact["attempt_count"]

        try:
            job_uuid = await originate.originate_call(contact["phone_number"], campaign["caller_id"])
            if job_uuid:
                self._job_to_contact[job_uuid] = (campaign_id, str(contact["id"]), max_attempts, attempt_count)
            else:
                # Accepted but FreeSWITCH didn't report a Job-UUID we can
                # track — can't resolve this one via the event listener,
                # so don't leave it stuck at 'calling' forever.
                log.warning(
                    "CampaignWorker: originate accepted with no Job-UUID contact=%s", contact["id"],
                )
                await self._resolve_with_retry(campaign_id, str(contact["id"]), max_attempts, attempt_count, "failed")
        except originate.OriginateError:
            log.exception("CampaignWorker: originate failed contact=%s", contact["id"])
            await self._resolve_with_retry(campaign_id, str(contact["id"]), max_attempts, attempt_count, "failed")

    async def _on_job_complete(self, job_uuid: str, succeeded: bool, detail: str) -> None:
        entry = self._job_to_contact.pop(job_uuid, None)
        if entry is None:
            return  # a BACKGROUND_JOB event for something this worker didn't originate — ignore
        campaign_id, contact_id, max_attempts, attempt_count = entry
        status = "completed" if succeeded else ("no_answer" if "NO_ANSWER" in detail else "failed")
        # On success `detail` is "+OK <channel-uuid>" — that channel UUID is
        # exactly what the Gateway uses as calls.session_id for this leg
        # (gateway/src/core/Application.cpp: ctx.obs.session_id = call_id,
        # the real FreeSWITCH channel UUID). Storing it here — even before
        # any calls row necessarily exists — lets the Admin UI join a
        # campaign contact to its call/transcript once the Gateway path
        # creates that row for the same UUID.
        call_session_id = detail.removeprefix("+OK").strip() or None if succeeded else None
        log.info(
            "CampaignWorker: job=%s contact=%s resolved status=%s call_session_id=%s detail=%s",
            job_uuid, contact_id, status, call_session_id, detail,
        )
        if status in ("failed", "no_answer"):
            await self._resolve_with_retry(campaign_id, contact_id, max_attempts, attempt_count, status)
        else:
            await self._resolve_contact(campaign_id, contact_id, status, call_session_id=call_session_id)

    async def _resolve_with_retry(
        self, campaign_id: str, contact_id: str, max_attempts: int, attempt_count: int, status: str,
    ) -> None:
        """A failed/no_answer contact gets requeued to 'pending' (so the
        normal claim path picks it up again, respecting pacing/concurrency
        same as any other contact) as long as campaigns.max_attempts
        hasn't been reached yet — otherwise it's left in its terminal
        failed/no_answer state, exhausted."""
        if attempt_count < max_attempts:
            log.info(
                "CampaignWorker: contact=%s attempt=%s/%s ended %s — requeueing for retry",
                contact_id, attempt_count, max_attempts, status,
            )
            await campaign_contacts.mark_contact_status(contact_id, "pending")
            self._in_flight[campaign_id] = max(0, self._in_flight.get(campaign_id, 1) - 1)
        else:
            log.info(
                "CampaignWorker: contact=%s exhausted after attempt=%s/%s (%s)",
                contact_id, attempt_count, max_attempts, status,
            )
            await self._resolve_contact(campaign_id, contact_id, status)

    async def _resolve_contact(
        self, campaign_id: str, contact_id: str, status: str, *, call_session_id: str | None = None,
    ) -> None:
        await campaign_contacts.mark_contact_status(contact_id, status, call_session_id=call_session_id)
        self._in_flight[campaign_id] = max(0, self._in_flight.get(campaign_id, 1) - 1)
