"""
ToolRegistry — static, DB-unaware catalog of ToolDefinition (schemas only).

Deliberately mirrors ai_provider_manager.py's _DEFAULT_REGISTRY shape: a
module-level dict populated at import time, not queried from Postgres.
Whether a given agent may actually USE a registered tool is
ToolPolicyResolver's job (policy_resolver.py), not this class's — see the
Tool Execution Framework design's review point 7 for why that split exists.

Adding a new tool means adding one entry to _DEFAULT_TOOLS here — never
touching ToolCallOrchestrator, ExecutorRegistry, or any ILLM implementation.
"""

from __future__ import annotations

from .types import ToolDefinition

# book_appointment — the only calendar-shaped tool the LLM ever sees.
# check_availability/find_available_slots are NOT here: they're private
# ICalendarProvider methods CalendarExecutor calls internally (see
# providers/calendar/ and executors/calendar_executor.py). event_type is
# deliberately absent too — one Cal.com event type per agent, configured in
# tool_provider_configs.extra.event_type_id, not an LLM decision in v1 (see
# the design doc's "intent -> event_type mapping" v2+ deferral).
#
# Phone-first identity (2026-07-27): attendee_phone is NOT required in this
# schema — CalendarExecutor uses the live call's own ANI automatically (see
# ToolExecutionContext.caller_number) and never asks. It only appears here
# as a fallback for sessions with no ANI at all (a webcall/browser test).
_BOOK_APPOINTMENT = ToolDefinition(
    name="book_appointment",
    description=(
        "Book a calendar appointment. Call this only once you know the date "
        "and time; ask first if either is missing. Never ask for email or "
        "phone number up front — the caller's number is already known from "
        "the call. "
        "If missing_fields includes attendee_phone with "
        "reason=invalid_phone_number, the number on file was invalid — ask "
        "the caller for a different one and call this tool again with it. "
        "If missing_fields includes attendee_phone with "
        "reason=phone_not_confirmed, you have not actually gotten the "
        "caller to confirm their number on file yet — state it back one "
        "digit at a time and ask if it's the best number to reach them, "
        "then wait for a clear yes (or a corrected number, read back the "
        "same way) before calling this tool again. Do not call this tool "
        "again until you have that clear confirmation. "
        "If missing_fields includes requested_datetime with "
        "reason=date_not_confirmed, the day-of-month you used does not match "
        "any number the caller actually said — you likely miscalculated it. "
        "Ask the caller to state the date again, then call this tool again "
        "with a day-of-month that matches what they say. "
        "If missing_fields includes requested_datetime with "
        "reason=date_in_past, the date you used has already passed — ask the "
        "caller which upcoming date they mean and call this tool again with "
        "a future date. "
        "If missing_fields includes requested_datetime with "
        "reason=date_too_far_out, you likely miscalculated the year — ask the "
        "caller to confirm the date again and call this tool again with a "
        "date within the next year. "
        "If missing_fields includes requested_datetime with "
        "reason=time_not_confirmed, the caller has not actually told you a "
        "time of day yet — even if they gave a date (e.g. \"tomorrow\"), a "
        "date alone is not enough. Ask specifically what time works for "
        "them, get a real answer (a clock time, or morning/afternoon/"
        "evening), and only then call this tool again. Never invent a time "
        "the caller didn't say. "
        "If missing_fields includes requested_datetime with "
        "reason=invalid_date_format, the value you sent was not a real "
        "date — e.g. leaving a placeholder like \"YYYY\" instead of an "
        "actual year, or otherwise malformed ISO 8601. Never write a "
        "placeholder token in place of a real number. Recompute the actual "
        "date and time from what the caller said and call this tool again "
        "with a fully resolved, real value. "
        "If booked=false with an available_slots list, the requested time "
        "was not available — that is not a booking. Offer one or two of "
        "those slots, and once the caller picks a new time, call this tool "
        "again with it. Never say something is booked unless the MOST "
        "RECENT call to this tool returned booked=true — repeating an "
        "earlier booked=true after a later attempt failed is the same "
        "error as never calling the tool. "
        "Before every call to this tool, restate the full resolved date back "
        "to the caller using the day name and calendar date together (e.g. "
        "\"just to confirm, that's Thursday, September 11th at 2 PM\") using "
        "the lookup table above, and wait for a clear yes before calling — "
        "the same way you confirm a phone number digit by digit. This "
        "catches your own date mistakes before they reach the calendar: "
        "confirmed live, this model has silently miscalculated a caller's "
        "stated date before (heard \"11th\", called this tool with the "
        "13th) — restating the date back is what would have caught that. "
        "Skip this only when the caller, in their very last turn, already "
        "repeated back the exact same day name and calendar date you're "
        "about to use. If you offered the caller more than one time option, "
        "a bare 'yes'/'sure' does not say which one they mean either — the "
        "same restate-and-wait rule applies. "
        "You must actually invoke this function to book anything — never "
        "say an appointment is booked, confirmed, or scheduled unless this "
        "function was called and returned that result; describing a "
        "booking in words instead is a serious error."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "attendee_name": {
                "type": ["string", "null"],
                "description": "The caller's name, if given.",
            },
            "attendee_phone": {
                "type": ["string", "null"],
                "description": (
                    "The caller's phone number — only needed if the tool tells you it's required "
                    "and you don't already have one on file for this call, or if a previous attempt "
                    "was rejected for reason=invalid_phone_number (ask for a different number in "
                    "that case, not the same one again). Never ask for this up front."
                ),
            },
            "requested_datetime": {
                "type": "string",
                "description": (
                    "ISO 8601 date and time the caller wants, e.g. 2026-07-23T15:00:00 — always in "
                    "the business's own local time, never the caller's. This is an in-person "
                    "appointment at a single physical location; what timezone the caller happens to "
                    "be calling from is irrelevant to when the appointment actually happens."
                ),
            },
            "notes": {
                "type": ["string", "null"],
                "description": "Any relevant detail the caller mentioned about the appointment.",
            },
        },
        "required": ["requested_datetime"],
    },
    category="calendar",
    # If this agent also has "send_sms" enabled, CalendarExecutor gets that
    # provider too — see ToolCallOrchestrator's generic companion
    # resolution and ToolDefinition.companion_tool_name's own docstring.
    companion_tool_name="send_sms",
)


# cancel_appointment (2026-07-23, phone-based since 2026-07-27) — resolves
# "cancel my appointment" into a concrete booking via a caller-STATED phone
# number (see CancelAppointmentExecutor); never exposes booking_id/uid to
# the LLM, since a real caller never has that memorized. Deliberately
# always asks — never silently uses the live call's own ANI, since a
# caller may be phoning in from a different number than the one they
# booked with. requested_datetime_hint is optional, only useful to
# disambiguate when the caller has more than one upcoming booking.
_CANCEL_APPOINTMENT = ToolDefinition(
    name="cancel_appointment",
    description=(
        "Cancel an existing appointment for the caller. Always ask for the phone number "
        "they booked with, even if you already know the number they're calling from now — "
        "it may not be the same one."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "attendee_phone": {
                "type": "string",
                "description": "The phone number the caller booked with — required to find their appointment.",
            },
            "requested_datetime_hint": {
                "type": ["string", "null"],
                "description": (
                    "The date/time the caller believes their appointment is for, if they mention one — "
                    "helps disambiguate when they have more than one upcoming appointment. Optional."
                ),
            },
        },
        "required": ["attendee_phone"],
    },
    category="calendar",
)


# reschedule_appointment (2026-07-23, phone-based since 2026-07-27) — same
# find-by-phone resolution as cancel_appointment (same "always ask, never
# trust the live ANI" reasoning), plus a new_requested_datetime for where
# to move it to. Cal.com's reschedule is atomic (one API call, see
# providers/calendar/interface.py's module docstring), so this executor
# doesn't compose cancel+book itself.
_RESCHEDULE_APPOINTMENT = ToolDefinition(
    name="reschedule_appointment",
    description=(
        "Move the caller's existing appointment to a new date/time. Always ask for the phone number "
        "they booked with, even if you already know the number they're calling from now. "
        "Before every call to this tool, restate the full resolved new date back to the caller using "
        "the day name and calendar date together (e.g. \"just to confirm, that's Thursday, September "
        "11th at 2 PM\") using the current-date lookup table, and wait for a clear yes before calling "
        "— the same way you confirm a phone number digit by digit. This catches your own date "
        "mistakes before they reach the calendar. Skip this only when the caller, in their very last "
        "turn, already repeated back the exact same day name and calendar date you're about to use. "
        "If missing_fields includes new_requested_datetime with reason=date_not_confirmed, the "
        "day-of-month you used does not match any number the caller actually said — ask them to state "
        "the date again and call this tool again with a day-of-month that matches. If "
        "missing_fields includes new_requested_datetime with reason=date_in_past, the date you used "
        "has already passed — ask which upcoming date they mean and call this again with a future date. "
        "If missing_fields includes new_requested_datetime with reason=date_too_far_out, you likely "
        "miscalculated the year — ask the caller to confirm the date again and call this again with a "
        "date within the next year. "
        "If missing_fields includes new_requested_datetime with reason=time_not_confirmed, the caller "
        "has not actually told you a new time of day yet — even if they gave a date, a date alone is "
        "not enough. Ask specifically what time works for them and only then call this again. Never "
        "invent a time the caller didn't say. "
        "If missing_fields includes new_requested_datetime with reason=invalid_date_format, the value "
        "you sent was not a real date — e.g. leaving a placeholder like \"YYYY\" instead of an actual "
        "year. Never write a placeholder token in place of a real number — recompute the actual date "
        "and time from what the caller said and call this again with a fully resolved, real value. "
        "If rescheduled=false with reason=requested_time_unavailable and an available_slots list, the "
        "new time was not free — offer one or two of those slots and call this again once the caller "
        "picks one. If reason=no_upcoming_booking, tell the caller you couldn't find an appointment "
        "under that number and ask them to double-check it. If reason=multiple_bookings_found, read "
        "back the upcoming_bookings dates and ask which one they mean, then call this again with a "
        "requested_datetime_hint. "
        "You must actually invoke this function to move anything — never say an appointment has been "
        "moved, rescheduled, or confirmed for a new time unless the MOST RECENT call to this function "
        "returned rescheduled=true; describing a reschedule in words instead, or repeating an earlier "
        "rescheduled=true after a later attempt failed, is a serious error."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "attendee_phone": {
                "type": "string",
                "description": "The phone number the caller booked with — required to find their appointment.",
            },
            "new_requested_datetime": {
                "type": "string",
                "description": (
                    "ISO 8601 date and time the caller wants to move their appointment to, e.g. "
                    "2026-07-23T15:00:00 — always in the business's own local time, never the caller's "
                    "(see book_appointment's requested_datetime for why)."
                ),
            },
            "requested_datetime_hint": {
                "type": ["string", "null"],
                "description": (
                    "The date/time the caller believes their CURRENT appointment is for, if they mention "
                    "one — helps disambiguate when they have more than one upcoming appointment. Optional."
                ),
            },
        },
        "required": ["attendee_phone", "new_requested_datetime"],
    },
    category="calendar",
)

# send_sms is admin-configurable (its own tool_provider_config/
# agent_tool_policies row, same as book_appointment) but llm_visible=False
# keeps it out of the schemas list orchestrator.py offers the LLM — it's a
# deterministic side effect CalendarExecutor triggers itself after a
# successful booking, never something the model decides to call. This
# entry exists only so ToolPolicyResolver.enabled_tools()'s registry
# lookup for tool_name="send_sms" succeeds; its schema content is never
# read since it never reaches an LLM.
_SEND_SMS = ToolDefinition(
    name="send_sms",
    description="Internal — never LLM-callable; sends a booking confirmation text.",
    parameters_schema={},
    category="notifications",
    llm_visible=False,
)

_DEFAULT_TOOLS: dict[str, ToolDefinition] = {
    _BOOK_APPOINTMENT.name: _BOOK_APPOINTMENT,
    _CANCEL_APPOINTMENT.name: _CANCEL_APPOINTMENT,
    _RESCHEDULE_APPOINTMENT.name: _RESCHEDULE_APPOINTMENT,
    _SEND_SMS.name: _SEND_SMS,
}


class ToolRegistry:
    def __init__(self, tools: dict[str, ToolDefinition] | None = None) -> None:
        self._tools = dict(_DEFAULT_TOOLS) if tools is None else dict(tools)

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition

    def resolve(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())
