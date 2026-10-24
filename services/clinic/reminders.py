"""Reminder calls: dial the patient through FreeSWITCH and hand the answered call to the
reminder agent's DID in the local dialplan.

The patient's number rides along in the `yuviz_remote_ani` channel variable, which
start_voice_ai.lua reports as the caller: the reminder agent's tools then find the
appointment by caller ID exactly as on an inbound call. Channel variables are set here,
server side, never from a SIP header, so a caller cannot forge one.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
from typing import Any

from libs.tenancy import platform_conn, tenant_conn
from services.clinic.esl_originate import OriginateError, _read_until_blank_line, is_valid_dial_number

from . import booking, db, notifications, reminders_schedule

log = logging.getLogger(__name__)

_POLL_S = 30.0
_ANSWER_TIMEOUT_S = 40
_NAMED_EXTENSION_RE = re.compile(r"[a-z][a-z0-9-]{2,40}")


def _esl_settings() -> tuple[str, int, str]:
    return (os.environ.get("FREESWITCH_ESL_HOST", "127.0.0.1"),
            int(os.environ.get("FREESWITCH_ESL_PORT", "8022")),
            os.environ.get("FREESWITCH_ESL_PASSWORD", ""))


def dial_number(e164: str) -> str:
    """What the carrier expects. CLINIC_DIAL_FORMAT: 'national0' (+919876543210 -> 09876543210,
    the default for this carrier), 'national' (9876543210) or 'e164'."""
    fmt = os.environ.get("CLINIC_DIAL_FORMAT", "national0")
    digits = re.sub(r"\D", "", e164)
    if fmt == "e164":
        return "+" + digits
    national = digits[2:] if digits.startswith("91") and len(digits) == 12 else digits
    return ("0" + national) if fmt == "national0" else national


def originate_command(*, patient_e164: str, agent_did: str, caller_id: str, gateway: str) -> str:
    patient_dial = dial_number(patient_e164)
    remote_ani = re.sub(r"\D", "", patient_e164)
    # A named, internal-only extension (no carrier can dial it), or a phone-number DID.
    if _NAMED_EXTENSION_RE.fullmatch(agent_did):
        did = agent_did
    else:
        did = re.sub(r"\D", "", agent_did)
        if not is_valid_dial_number(did):
            raise OriginateError(f"refusing to dial: did {agent_did!r} is neither a number nor an extension name")
    for label, value in (("patient", patient_dial), ("caller_id", caller_id)):
        if not is_valid_dial_number(value):
            raise OriginateError(f"refusing to dial: {label} {value!r} is not a plain dial number")
    if not re.fullmatch(r"[0-9A-Za-z.\-]+:[0-9]{1,5}", gateway):
        raise OriginateError("CLINIC_REMINDER_SIP_GATEWAY must be host:port")
    # The sofia profile whose SDP the carrier can reach: one advertising a STUN public IP to a
    # carrier on the private network gives one-way audio (the agent hears nothing).
    profile = os.environ.get("CLINIC_REMINDER_SIP_PROFILE", "external")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", profile):
        raise OriginateError("CLINIC_REMINDER_SIP_PROFILE must be a sofia profile name")
    variables = ",".join((
        f"origination_caller_id_number={caller_id}",
        f"originate_timeout={_ANSWER_TIMEOUT_S}",
        "ignore_early_media=true",
        f"yuviz_remote_ani={remote_ani}",
    ))
    command = f"bgapi originate {{{variables}}}sofia/{profile}/sip:{patient_dial}@{gateway} {did} XML public"
    if any(c in command for c in "\r\n\0"):
        raise OriginateError("refusing to send an ESL command containing CR/LF/NUL")
    return command


async def send_originate(command: str) -> str:
    host, port, password = _esl_settings()
    if not password:
        raise OriginateError("FREESWITCH_ESL_PASSWORD is not set")
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        raise OriginateError(f"cannot reach FreeSWITCH ESL at {host}:{port}: {exc}") from exc
    try:
        await _read_until_blank_line(reader)
        writer.write(f"auth {password}\n\n".encode())
        await writer.drain()
        if (await _read_until_blank_line(reader)).get("Reply-Text", "").strip() != "+OK accepted":
            raise OriginateError("ESL auth rejected")
        writer.write(f"{command}\n\n".encode())
        await writer.drain()
        reply_text = (await _read_until_blank_line(reader)).get("Reply-Text", "")
        if not reply_text.startswith("+OK"):
            raise OriginateError(f"originate rejected: {reply_text}")
        return reply_text.split("Job-UUID:")[-1].strip() if "Job-UUID:" in reply_text else ""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _queue_reminder_sms(conn: Any, clinic: booking.Clinic, appointment: dict) -> None:
    doctor = await booking._doctor_by_id(conn, clinic, appointment["doctor_id"])
    if doctor is None:
        return
    await notifications.queue_sms(
        conn, settings=clinic.settings, appointment=appointment, doctor=doctor,
        department=booking._department_of(doctor), local_start=clinic.local(appointment["starts_at"]),
        event="reminder",
    )


class ReminderWorker:
    def __init__(self, *, originate=send_originate) -> None:
        self._originate = originate
        self._task: asyncio.Task | None = None
        self._listener = None

    def start(self) -> None:
        from services.clinic.esl_originate import EslJobEventListener

        self._listener = EslJobEventListener(self.on_job_complete)
        self._listener.start()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._listener is not None:
            await self._listener.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("clinic reminders: tick failed")
            await asyncio.sleep(_POLL_S)

    async def tick(self, *, now: datetime.datetime | None = None) -> int:
        pool = await db.get_pool()
        # Cross-tenant by design: one worker serves every clinic. Rows are leased by pushing
        # next_reminder_at forward, so a crash mid-dial retries later instead of never.
        async with platform_conn(pool, reason="clinic-reminder-claim") as conn:
            due = await conn.fetch(
                "UPDATE clinic_appointments a SET next_reminder_at = now() + interval '10 minutes' "
                "FROM clinic_settings s "
                "WHERE a.id IN (SELECT a2.id FROM clinic_appointments a2 JOIN clinic_settings s2 USING (tenant_id) "
                "               WHERE a2.status = 'booked' AND a2.next_reminder_at <= now() "
                "                 AND a2.reminder_status IN ('pending', 'calling') AND s2.reminder_enabled "
                "               ORDER BY a2.next_reminder_at LIMIT 5 FOR UPDATE OF a2 SKIP LOCKED) "
                "  AND s.tenant_id = a.tenant_id "
                "RETURNING a.id, a.tenant_id",
            )
        for row in due:
            await self._dial_one(str(row["tenant_id"]), row["id"], now=now)
        return len(due)

    async def _dial_one(self, tenant_id: str, appointment_id: Any, *, now: datetime.datetime | None) -> None:
        pool = await db.get_pool()
        async with tenant_conn(pool, explicit_tenant=tenant_id, reason="clinic-reminder-dial") as conn:
            clinic = await booking.load_clinic(conn, tenant_id, now=now)
            appointment = dict(await conn.fetchrow("SELECT * FROM clinic_appointments WHERE id = $1", appointment_id))
            settings = clinic.settings
            attempt = appointment["reminder_attempts"] + 1
            gateway = os.environ.get("CLINIC_REMINDER_SIP_GATEWAY", "")
            if attempt > settings["reminder_max_attempts"] or not (
                settings.get("reminder_agent_did") and settings.get("reminder_caller_id") and gateway
            ):
                # Out of attempts, or calls are not set up: an SMS reminder is the fallback.
                await conn.execute(
                    "UPDATE clinic_appointments SET reminder_status = 'unreachable', next_reminder_at = NULL, "
                    "updated_at = now() WHERE id = $1", appointment_id,
                )
                await _queue_reminder_sms(conn, clinic, appointment)
                return
            retry = reminders_schedule.retry_at(clinic.now, appointment["starts_at"], settings, clinic.tz)
            await conn.execute(
                "UPDATE clinic_appointments SET reminder_status = 'calling', reminder_attempts = $2, "
                "next_reminder_at = $3, updated_at = now() WHERE id = $1",
                appointment_id, attempt, retry,
            )
            if retry is None:
                # No time left for another try after this one: send the SMS now as well.
                await _queue_reminder_sms(conn, clinic, appointment)
            call_row = await conn.fetchrow(
                "INSERT INTO clinic_reminder_calls (tenant_id, appointment_id, attempt) VALUES ($1, $2, $3) "
                "RETURNING id", tenant_id, appointment_id, attempt,
            )
            try:
                command = originate_command(
                    patient_e164=appointment["patient_phone"], agent_did=settings["reminder_agent_did"],
                    caller_id=settings["reminder_caller_id"], gateway=gateway,
                )
                job_uuid = await self._originate(command)
            except OriginateError as exc:
                log.warning("clinic reminders: originate failed ref=%s: %s", appointment["reference"], exc)
                await conn.execute(
                    "UPDATE clinic_reminder_calls SET status = 'failed', detail = $2, finished_at = now() "
                    "WHERE id = $1", call_row["id"], str(exc)[:300],
                )
                return
            await conn.execute("UPDATE clinic_reminder_calls SET job_uuid = $2 WHERE id = $1", call_row["id"], job_uuid)
            log.info("clinic reminders: dialing ref=%s attempt=%d job=%s", appointment["reference"], attempt, job_uuid)

    async def on_job_complete(self, job_uuid: str, succeeded: bool, detail: str) -> None:
        """BACKGROUND_JOB for an originate: +OK means the patient answered and the call is now
        with the reminder agent; anything else (NO_ANSWER, USER_BUSY...) waits for the retry."""
        pool = await db.get_pool()
        async with platform_conn(pool, reason="clinic-reminder-job-event") as conn:
            row = await conn.fetchrow(
                "UPDATE clinic_reminder_calls SET status = $2, detail = $3, finished_at = now() "
                "WHERE job_uuid = $1 AND status = 'dialing' RETURNING tenant_id, appointment_id",
                job_uuid, "answered" if succeeded else "no_answer", detail[:300],
            )
            if row is None:
                return  # a campaign's job, not ours
            if succeeded:
                # Answered: no more dialling. The agent records confirmed/declined if the caller says.
                await conn.execute(
                    "UPDATE clinic_appointments SET next_reminder_at = NULL, updated_at = now() "
                    "WHERE id = $1 AND reminder_status = 'calling'", row["appointment_id"],
                )
