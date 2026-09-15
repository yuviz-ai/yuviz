"""
CalendarExecutor — the only IToolExecutor the LLM's book_appointment tool
call ever reaches. Availability checking, alternative-slot lookup, and the
actual booking call are all internal to execute() — ToolCallOrchestrator
sees exactly one execute(request) -> ToolResult call, identical in shape to
any other tool (see Tool Execution Framework design §12).

Every exit point returns a ToolResult; nothing here raises out to the
middleware chain except a genuinely unexpected bug.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..providers.calendar.interface import (
    AttendeeInfo, CalendarProviderError, ICalendarProvider, InvalidAttendeePhoneError, SlotUnavailableError,
)
from ..providers.sms.interface import ISmsProvider
from ..types import ToolExecutionRequest, ToolResult, ToolStatus

log = logging.getLogger(__name__)


def _to_e164(phone: str, caller_number: str | None) -> str:
    """Prepends a country code to a bare national number the LLM collected
    digit-by-digit from the caller, using the caller's own ANI as the
    source of the code — Cal.com's /v2/bookings requires E.164
    (attendee.phoneNumber), but a caller stating their number aloud never
    includes one. A correctly-transcribed, correctly-confirmed national
    number sent with no country code comes back invalid_number from
    Cal.com even though nothing was wrong with the number itself — it was
    a formatting gap, not a phone-number-accuracy problem. The prefix is
    only borrowed when the ANI actually ends with the digits the caller
    stated — i.e. it is the same number, just spoken without its country
    code. Any other number (an alternate contact, a mistranscription) has
    no reliable prefix to borrow, so the raw value is returned unchanged
    rather than guessing a country wrong silently."""
    if not phone or phone.startswith("+"):
        return phone
    digits = "".join(c for c in phone if c.isdigit())
    # A short or empty digit string matches almost any ANI by chance (an
    # empty string is a suffix of everything) — that's not "the same
    # number spoken without its country code," it's a coincidence. No real
    # national number is shorter than this; below it, treat as unresolvable
    # rather than splice a fragment onto someone else's country code.
    if len(digits) < 7:
        return phone
    if not caller_number or not caller_number.startswith("+"):
        return phone
    caller_digits = "".join(c for c in caller_number if c.isdigit())
    # The borrowed prefix must actually look like a country code (1-3
    # digits) — a same-length or near-length match isn't "no country code
    # was given," it's a different number entirely, and a huge gap means
    # `digits` is too short to be a real national number in the first place.
    prefix_len = len(caller_digits) - len(digits)
    if not 1 <= prefix_len <= 3 or not caller_digits.endswith(digits):
        return phone
    country_code = caller_digits[:prefix_len]
    return f"+{country_code}{digits}"

# Holds references to in-flight fire-and-forget SMS sends — asyncio only
# weakly tracks a task once nothing else holds it, so without this a task
# can be garbage-collected mid-send under GC pressure, silently dropping
# an already-started text with no error (see _send_confirmation_sms).
_pending_sms_sends: set[asyncio.Task] = set()


def _format_when(requested_datetime: str) -> str:
    """requested_datetime is the same naive, business-local wall-clock
    string that was actually booked (see book_appointment's own schema
    description) — formatting it directly, rather than re-deriving a
    human-readable time from Cal.com's returned UTC confirmed_slot, avoids
    a second timezone conversion that could introduce its own bug."""
    try:
        dt = datetime.fromisoformat(requested_datetime)
        return dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
    except ValueError:
        return requested_datetime


def _speak_confirmation(requested_datetime: str, sms_attempted: bool = False) -> str:
    base = f"You're all set — I've booked your appointment for {_format_when(requested_datetime)}."
    if sms_attempted:
        # Future tense — an honest claim regardless of outcome, since the
        # send fires in the background and isn't awaited (see
        # _send_confirmation_sms). "I've sent" would be a claim about
        # something that hasn't necessarily happened yet.
        base += " I'll also text you a confirmation."
    return base


def _tz_abbreviation(requested_datetime: str, tz: str) -> str:
    """Timezone abbreviation (e.g. IST) for the SMS text, read later out of
    context unlike the spoken confirmation. tz is the business's own
    IANA zone (see execute()), never the caller's."""
    try:
        dt = datetime.fromisoformat(requested_datetime).replace(tzinfo=ZoneInfo(tz))
        return dt.strftime("%Z")
    except (ValueError, KeyError):
        return ""


def _sms_confirmation(requested_datetime: str, tz: str) -> str:
    when = _format_when(requested_datetime)
    abbr = _tz_abbreviation(requested_datetime, tz)
    if abbr:
        when = f"{when} {abbr}"
    return f"Your appointment is confirmed for {when}. See you then!"


class CalendarExecutor:
    def __init__(self, provider: ICalendarProvider, sms_provider: ISmsProvider | None = None) -> None:
        self._provider = provider
        self._sms_provider = sms_provider

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        args = request.arguments

        requested_datetime = args.get("requested_datetime")
        if not requested_datetime:
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={"missing_fields": ["requested_datetime"]})

        # requested_datetime is always in the business's own local time (see
        # the tool schema) — never the caller's. An in-person appointment at
        # a single physical location happens in that location's timezone
        # regardless of where the caller is phoning from; letting the LLM
        # supply a caller-guessed timezone here caused every non-local-tz
        # caller's booking to silently miscompare against real Cal.com slots
        # and never post (found live 2026-08-01).
        tz = self._provider.default_timezone

        # A real telephony call always has a caller_number (ANI) — used
        # automatically so the agent never has to ask up front. BUT: an
        # explicit attendee_phone argument, when present, must win over the
        # ANI, not the other way around — the LLM only ever populates that
        # argument when it was told to ask (no ANI at all, or the ANI was
        # just rejected by InvalidAttendeePhoneError below). Found live
        # 2026-07-30: with the old (ani or args) priority, every retry
        # after an invalid-ANI rejection silently kept resending the same
        # already-rejected ANI, ignoring whatever real number the caller
        # had just stated — the retry path could never actually succeed.
        attendee_phone = (args.get("attendee_phone") or request.context.caller_number or "").strip()
        attendee_phone = _to_e164(attendee_phone, request.context.caller_number)

        # Deterministic gate (not just a prompt instruction) — an LLM can
        # silently skip confirming the ANI and book anyway. Only applies
        # when attendee_phone fell back to the ANI, not an explicit arg
        # (that's a caller-just-stated-it-this-turn case, covered by the
        # system prompt's own digit-confirmation guardrail instead).
        if (
            not args.get("attendee_phone")
            and request.context.caller_number
            and not request.context.phone_number_confirmed
        ):
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                "missing_fields": ["attendee_phone"],
                "reason": "phone_not_confirmed",
            })

        # Business-rule-level validation: only ask the caller when the
        # provider genuinely requires a phone number AND no tenant-
        # configured default exists AND we don't already have the ANI.
        if (
            self._provider.requires_attendee_phone()
            and not attendee_phone
            and not self._provider.default_attendee_phone
        ):
            return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={"missing_fields": ["attendee_phone"]})

        attendee = AttendeeInfo(
            name=args.get("attendee_name") or "Caller",
            phone=attendee_phone,
            timezone=tz,
        )

        try:
            available = await self._provider.check_availability(requested_datetime, tz)
        except CalendarProviderError:
            log.exception("CalendarExecutor: check_availability failed call_id=%s", request.tool_call_id)
            return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        if available:
            try:
                booking = await self._provider.book_appointment(
                    requested_datetime, attendee, notes=args.get("notes", ""),
                )
                sms_attempted = self._send_confirmation_sms(
                    attendee.phone, requested_datetime, tz, request.tool_call_id,
                )
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    payload={
                        "booked": True,
                        "booking_id": booking.booking_id,
                        "confirmed_slot": booking.confirmed_slot,
                        "meeting_url": booking.meeting_url,
                    },
                    deterministic_response=_speak_confirmation(requested_datetime, sms_attempted=sms_attempted),
                    confirmed_datetime=requested_datetime,
                )
            except SlotUnavailableError:
                # Race: check_availability said yes, but the slot was taken
                # before book() landed — treated identically to an upfront
                # "not available" (see design §12), not FAILED.
                log.info("CalendarExecutor: booking conflict (race) call_id=%s", request.tool_call_id)
            except InvalidAttendeePhoneError:
                # The ANI (or a caller-stated number) reached Cal.com fine
                # but failed ITS phone-number validation — e.g. a caller ID
                # that isn't a real phone number at all (confirmed live
                # 2026-07-29 with a SIP test extension). Distinct from
                # "no phone number at all": same recovery shape
                # (missing_fields asks the LLM to collect attendee_phone
                # and retry) but with a reason so the agent doesn't sound
                # like it's asking for a phone number for the first time —
                # it's telling the caller the one on file didn't work.
                log.info(
                    "CalendarExecutor: attendee phone rejected as invalid call_id=%s", request.tool_call_id,
                )
                return ToolResult(status=ToolStatus.INVALID_ARGUMENT, payload={
                    "missing_fields": ["attendee_phone"],
                    "reason": "invalid_phone_number",
                })
            except CalendarProviderError:
                log.exception("CalendarExecutor: book_appointment failed call_id=%s", request.tool_call_id)
                return ToolResult(status=ToolStatus.FAILED, error="calendar_error")

        try:
            slots = await self._provider.find_available_slots(requested_datetime, tz)
            return ToolResult(status=ToolStatus.SUCCESS, payload={
                "booked": False,
                "available_slots": [s.start_iso for s in slots],
            })
        except CalendarProviderError:
            # The core "not available" answer is still valid business data
            # even if alternatives couldn't be fetched — degrade to an
            # empty list, not FAILED (see design §12).
            log.exception("CalendarExecutor: find_available_slots failed call_id=%s", request.tool_call_id)
            return ToolResult(status=ToolStatus.SUCCESS, payload={"booked": False, "available_slots": []})

    def _send_confirmation_sms(
        self, to_number: str, requested_datetime: str, tz: str, tool_call_id: str,
    ) -> bool:
        # Fire-and-forget — never awaited, so this adds zero latency to the
        # spoken confirmation. Return value means "an attempt was fired,"
        # not "it succeeded" (see _speak_confirmation's future-tense
        # wording, which is honest either way). No provider/no ANI just
        # skips silently.
        if self._sms_provider is None or not to_number:
            return False

        def _on_done(t: asyncio.Task) -> None:
            _pending_sms_sends.discard(t)
            # Any ISmsProvider implementation, not just Twilio's own
            # SmsProviderError — retrieved so it isn't reported as
            # "exception never retrieved", never re-raised (a failed text
            # must never look like a failed booking).
            exc = t.exception() if not t.cancelled() else None
            if exc is not None:
                log.error("CalendarExecutor: confirmation SMS failed call_id=%s: %s", tool_call_id, exc)

        send = asyncio.ensure_future(
            self._sms_provider.send_sms(to_number, _sms_confirmation(requested_datetime, tz))
        )
        # Held in the module-level set until done — otherwise nothing keeps
        # this task alive once _send_confirmation_sms returns.
        _pending_sms_sends.add(send)
        send.add_done_callback(_on_done)
        return True
