from datetime import datetime, timedelta, timezone

from ..tools.date_sanity import (
    correct_year_if_wrong,
    date_field_for_tool,
    is_in_the_past,
    no_time_stated,
    parse_requested_date,
    stated_day_mismatch,
)


def test_date_field_for_tool_maps_the_two_calendar_mutating_tools():
    assert date_field_for_tool("book_appointment") == "requested_datetime"
    assert date_field_for_tool("reschedule_appointment") == "new_requested_datetime"
    assert date_field_for_tool("cancel_appointment") is None


def test_stated_day_mismatch_true_when_digit_present_and_different():
    assert stated_day_mismatch(15, "book it for the 20th please") is True


def test_stated_day_mismatch_false_when_digit_matches():
    assert stated_day_mismatch(15, "the 15th of March works") is False


def test_stated_day_mismatch_false_with_no_digit_at_all():
    # Relative phrasing ("tomorrow") has no digit to cross-check — that's
    # the current-date lookup table's job, not this check's.
    assert stated_day_mismatch(15, "tomorrow would be great") is False


def test_stated_day_mismatch_ignores_out_of_range_numbers():
    # A 4-digit year or an unrelated large number should never be treated
    # as a plausible day-of-month candidate.
    assert stated_day_mismatch(15, "call me back around 2026 or so") is False


def test_is_in_the_past_true_for_a_clearly_past_date():
    assert is_in_the_past(datetime(2020, 1, 1), "UTC") is True


def test_is_in_the_past_false_for_a_future_date():
    future = datetime.now(timezone.utc) + timedelta(days=30)
    assert is_in_the_past(future.replace(tzinfo=None), "UTC") is False


def test_is_in_the_past_falls_back_to_utc_on_unknown_timezone():
    assert is_in_the_past(datetime(2020, 1, 1), "Not/AZone") is True


def test_is_in_the_past_falls_back_to_utc_on_malformed_timezone_key():
    # ZoneInfo raises ValueError (not ZoneInfoNotFoundError) for a
    # malformed key like a trailing slash — a misconfigured agent must
    # still degrade to UTC, not take every call for that agent down.
    assert is_in_the_past(datetime(2020, 1, 1), "Asia/Kolkata/") is True


def test_parse_requested_date_handles_malformed_input_without_raising():
    assert parse_requested_date("not-a-date") is None
    assert parse_requested_date(None) is None
    assert parse_requested_date(12345) is None


def test_parse_requested_date_parses_iso_format():
    assert parse_requested_date("2026-09-10T14:00:00") == datetime(2026, 9, 10, 14, 0, 0)


def test_no_time_stated_true_when_nothing_ever_mentions_a_time():
    # Confirmed live 2026-09-09: a caller who never mentioned a time still
    # got a real booking, at a time the LLM invented on its own.
    assert no_time_stated(["I'm interested", "tomorrow would be good", "yes that's correct"]) is True


def test_no_time_stated_false_for_am_pm():
    assert no_time_stated(["how about 2pm tomorrow"]) is False
    assert no_time_stated(["2:30 PM works for me"]) is False


def test_no_time_stated_false_for_24_hour_format_with_dot_or_colon():
    assert no_time_stated(["10.30 would be fine"]) is False
    assert no_time_stated(["14:00 works"]) is False


def test_no_time_stated_false_for_part_of_day_words():
    assert no_time_stated(["sometime in the afternoon"]) is False
    assert no_time_stated(["tomorrow morning"]) is False


def test_no_time_stated_ignores_unrelated_numbers():
    # A phone number or a bare day-of-month must not be mistaken for a time.
    assert no_time_stated(["my number is 918971188211", "the 15th of March"]) is True


def test_no_time_stated_checks_every_turn_not_just_the_last():
    # The time may have been stated several turns before the tool call
    # finally fires — must not only look at the most recent utterance.
    assert no_time_stated(["let's do 3pm", "great, that's tomorrow then", "yes correct"]) is False


def test_no_time_stated_true_for_empty_history():
    assert no_time_stated([]) is True


def test_stated_day_mismatch_ignores_the_hour_in_a_stated_time():
    # "tomorrow at 2 PM" states no day-of-month at all — treating the hour
    # as one rejected a valid booking, and the widened window kept that
    # turn in scope so the rejection repeated every retry.
    assert stated_day_mismatch(15, "tomorrow at 2 PM works") is False
    assert stated_day_mismatch(15, "tomorrow at 2:00 pm works") is False
    assert stated_day_mismatch(15, "3 o'clock tomorrow is fine") is False


def test_stated_day_mismatch_still_catches_a_wrong_day_stated_alongside_a_time():
    assert stated_day_mismatch(15, "book it for the 20th at 2 PM") is True
    assert stated_day_mismatch(15, "the 15th of March at 2 PM") is False


def test_correct_year_leaves_a_past_date_that_already_has_the_current_year():
    # The year isn't what's wrong here — rolling it forward would silently
    # book a year out. Must reject (date_in_past) instead.
    this_year = datetime.now().year
    assert correct_year_if_wrong(datetime(this_year, 1, 1, 10, 0), "UTC") is None


def test_correct_year_still_fixes_a_genuinely_wrong_past_year():
    corrected = correct_year_if_wrong(datetime(datetime.now().year - 2, 12, 31, 10, 0), "UTC")
    assert corrected is not None and corrected.year >= datetime.now().year
