"""
Date sanity checks — a cheap, code-level backstop for an LLM reliability
gap: small local models keep miscalculating a caller-stated appointment
date, sometimes during relative-date math ("tomorrow") and, worse, even
when just transcribing an explicit day-of-month the caller literally just
said. A prompt instruction alone ("restate the date and wait for a yes" —
see registry.py) does not reliably stop it — the model can still call
book_appointment with a wrong date despite that instruction being in its
own tool description.

Three independent, side-effect-free checks, run in orchestrator.py before
the executor/middleware chain — so an already-known-bad date never spends
a real calendar API call or burns the tool's timeout budget:

  1. stated_day_mismatch: if the caller's last utterance states an
     explicit day-of-month numeral, the tool's requested date must use
     that same day. Skipped entirely when the utterance has no such
     numeral — a relative phrase like "tomorrow" has none, and resolving
     that is the current-date lookup table's job (pipeline.py's
     _build_current_date_context), not this check's.
  2. is_in_the_past: the requested date must not be before "today" in the
     calendar's own configured timezone — catches a miscalculation that
     happens to land in the past regardless of what the caller said.
  3. no_time_stated: catches a call where no time was ever stated by
     either party still getting a real booking, at a time the LLM invented
     on its own — a different failure mode than a wrong computation, and
     one the other two checks can't catch (there is no stated time to be
     wrong about). Scans every turn in the call so far, caller and agent
     both — an agent-proposed slot the caller accepts with a bare "yes"
     never appears in the caller's own words, but was genuinely stated
     and agreed to.

All three are deliberately narrow, false-negative-tolerant backstops —
same posture as pipeline.py's _claims_booking_without_tool_call — not a
replacement for the LLM getting the date/time right in the first place.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# tool_name -> the argument in ToolCallEvent.arguments holding the date the
# caller wants — the two tools whose date the caller states out loud.
# cancel_appointment's requested_datetime_hint is deliberately excluded: a
# hint describing an EXISTING appointment may legitimately be in the past
# (the caller may misremember it), and it does not create/move anything.
DATE_ARG_BY_TOOL: dict[str, str] = {
    "book_appointment": "requested_datetime",
    "reschedule_appointment": "new_requested_datetime",
}

_DAY_NUMERAL_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\b")

# Hour words alongside numerals — Deepgram can transcribe a spoken "two PM"
# as the word "two", not the digit "2", so a numerals-only pattern misses
# a clearly-stated time and no_time_stated() would wrongly reject a call
# whose caller did state a time, just not in numeral form.
_HOUR_WORD = r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"

# Deliberately specific constructs (am/pm, 24-hour HH:MM, "o'clock", named
# parts of day) — far less likely to false-positive on an unrelated number
# (a phone digit run, a street address) than a bare 1-2 digit numeral would.
_TIME_EXPRESSION_RE = re.compile(
    r"\b\d{1,2}([.:]\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b"     # "2 PM", "2:30pm", "10.30 a.m."
    rf"|\b{_HOUR_WORD}\s*(a\.?m\.?|p\.?m\.?)\b"          # "two PM", "ten a.m."
    r"|\b([01]?\d|2[0-3])[.:][0-5]\d\b"                  # 24-hour "14:00", "10.30"
    r"|\b\d{1,2}\s*o'?clock\b"                           # "3 o'clock"
    rf"|\b{_HOUR_WORD}\s*o'?clock\b"                     # "three o'clock"
    r"|\b(morning|afternoon|evening|noon|midnight|tonight)\b",
    re.IGNORECASE,
)


def date_field_for_tool(tool_name: str) -> str | None:
    return DATE_ARG_BY_TOOL.get(tool_name)


def stated_day_mismatch(day: int, caller_utterance: str) -> bool:
    """True only when caller_utterance contains at least one 1-2 digit
    numeral that could plausibly be a day-of-month (1-31) and NONE of them
    equal day. An utterance with no such numeral at all (a relative phrase,
    or a turn that's just a bare "yes") never trips this.

    Time expressions are removed before scanning: the hour in "tomorrow at
    2 PM" is not a day-of-month, and counting it as one rejected an
    entirely valid booking (day 15 vs a "stated day" of 2) and then kept
    rejecting it, since the widened window still sees that turn."""
    text = _TIME_EXPRESSION_RE.sub(" ", caller_utterance)
    candidates = [int(m) for m in _DAY_NUMERAL_RE.findall(text) if 1 <= int(m) <= 31]
    if not candidates:
        return False
    return day not in candidates


def no_time_stated(conversation_texts: list[str]) -> bool:
    """True when NONE of the turns so far (caller or agent) contain a
    recognizable time-of-day expression. Both roles matter, not just the
    caller's: an agent-proposed slot ("how about 2 PM?") the caller accepts
    with a bare "yes" never puts a time in the caller's own words, but a
    time was genuinely stated and agreed to. An empty list (a
    local-tools-only turn with no history passed) is treated the same as
    "nothing stated" — fail closed, not open."""
    return not any(_TIME_EXPRESSION_RE.search(u) for u in conversation_texts)


def is_in_the_past(dt: datetime, calendar_timezone: str) -> bool:
    try:
        tz = ZoneInfo(calendar_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()
    return dt.date() < today


# correct_year_if_wrong only ever rolls a past date forward — a model that
# guesses a year too far in the FUTURE (2027 instead of 2026) produces a
# date that is not in the past at all, so is_in_the_past never flags it and
# no correction is ever consulted. day-of-month/time can be exactly what
# the caller said and the booking still lands a year out with nothing to
# catch it. One year out covers any legitimate real booking (this platform
# has no scheduling-months-ahead use case today) while still catching the
# same "small model, wrong year" failure mode in the other direction.
_MAX_DAYS_IN_FUTURE = 365


def is_too_far_out(dt: datetime, calendar_timezone: str, max_days: int = _MAX_DAYS_IN_FUTURE) -> bool:
    try:
        tz = ZoneInfo(calendar_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()
    return (dt.date() - today).days > max_days


# How far off a guessed year can be and still count as the "small model
# can't compute the current year" failure mode (a 2-3 year gap) rather
# than a genuinely bogus date (e.g. a decade off, or a typo) that deserves
# outright rejection instead of being silently papered over.
_MAX_YEAR_CORRECTION_GAP = 5


def correct_year_if_wrong(dt: datetime, calendar_timezone: str) -> datetime | None:
    """If dt is in the past purely because of its year, and that year is a
    plausible near-miss (not a wildly bogus date), try the real current
    year and, failing that, next year, keeping month/day/time unchanged.
    Small models reliably get the day-of-month and time right when the
    caller states them but get the YEAR wrong by a couple of years —
    day-of-month is already cross-checked separately against what the
    caller actually said (stated_day_mismatch), so silently replacing only
    the year and re-validating is safe: it preserves the caller's actual
    stated intent and only replaces a label the model was never going to
    get right by computation anyway. Returns None if the year is not
    actually wrong (a past date already carrying the current year is a
    wrong day/month, not a wrong year — rolling it forward would silently
    book a year out), if the guessed year is too far off to be that
    failure mode, or if neither candidate year lands on a non-past date
    (Feb 29 landing on a non-leap year also falls through to None here,
    same as any other malformed date)."""
    try:
        tz = ZoneInfo(calendar_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    current_year = datetime.now(tz).year
    if not 0 < current_year - dt.year <= _MAX_YEAR_CORRECTION_GAP:
        return None
    for year in (current_year, current_year + 1):
        try:
            candidate = dt.replace(year=year)
        except ValueError:
            continue
        if not is_in_the_past(candidate, calendar_timezone):
            return candidate
    return None


def parse_requested_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
