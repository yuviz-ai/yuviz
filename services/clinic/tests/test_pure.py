"""Slot engine, spoken text, phone numbers and reminder timing — no database."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from services.clinic import booking, phones, reminders_schedule, slots, speech

IST = ZoneInfo("Asia/Kolkata")


def _at(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=IST)


# ── phones ──

def test_normalize_phone_accepts_indian_forms():
    for raw in ("+91 98765-43210", "09876543210", "919876543210", "9876543210", "+09876543210"):
        assert phones.normalize_phone(raw) == "+919876543210", raw


def test_normalize_phone_rejects_junk():
    for raw in (None, "", "12345", "abc", "+12"):
        assert phones.normalize_phone(raw) is None, raw


def test_normalize_phone_keeps_foreign_e164():
    assert phones.normalize_phone("+1 415 555 0100") == "+14155550100"


# ── speech ──

def test_spoken_dates_relative_and_absolute():
    today = datetime.date(2026, 10, 19)
    assert speech.spoken_when(_at(19, 11, 30), today, "en") == "today at 11:30 AM"
    assert speech.spoken_when(_at(20, 17), today, "en") == "tomorrow at 5 PM"
    assert speech.spoken_when(_at(23, 9, 15), today, "en") == "Friday, 23 October at 9:15 AM"


def test_spoken_dates_hindi():
    today = datetime.date(2026, 10, 19)
    assert speech.spoken_when(_at(20, 11, 30), today, "hi") == "कल सुबह 11:30 बजे"
    assert speech.spoken_when(_at(23, 17), today, "hi") == "शुक्रवार, 23 अक्टूबर शाम 5 बजे"
    assert speech.spoken_time(_at(20, 13), "hi") == "दोपहर 1 बजे"


def test_unknown_language_falls_back_to_english():
    assert speech.spoken_time(_at(20, 9), "fr") == "9 AM"


# ── slots ──

MON_MORNING = slots.ScheduleBlock(weekday=0, start_time=datetime.time(10), end_time=datetime.time(11))


def test_grid_has_no_partial_slot():
    grid = slots.slot_grid(
        [slots.ScheduleBlock(0, datetime.time(10), datetime.time(10, 50))], datetime.date(2026, 10, 19), 15, IST,
    )
    assert [s.strftime("%H:%M") for s, _ in grid] == ["10:00", "10:15", "10:30"]


def test_free_slots_skip_busy_leave_and_lead_time():
    free = slots.free_slots(
        blocks=[MON_MORNING], slot_minutes=15, tz=IST,
        first_day=datetime.date(2026, 10, 19), last_day=datetime.date(2026, 10, 26),
        not_before=_at(19, 10, 10),
        unavailable=[(_at(19, 10, 30), _at(19, 10, 45)),      # a booking
                     (_at(26, 0), _at(27, 0))],                # leave all next Monday
    )
    assert [s.strftime("%d %H:%M") for s, _ in free] == ["19 10:15", "19 10:45"]


def test_schedule_validity_window():
    block = slots.ScheduleBlock(0, datetime.time(10), datetime.time(11), valid_from=datetime.date(2026, 10, 26))
    assert not block.applies_on(datetime.date(2026, 10, 19))
    assert block.applies_on(datetime.date(2026, 10, 26))


def test_pick_for_offer_spreads_across_sessions():
    day = [(_at(19, h, m), _at(19, h, m) + datetime.timedelta(minutes=15))
           for h in (10, 11, 15, 17) for m in (0, 15, 30)]
    picked = slots.pick_for_offer(day, limit=4)
    assert [s.strftime("%H:%M") for s, _ in picked] == ["10:00", "10:15", "15:00", "15:15"]
    evening = slots.pick_for_offer(day, limit=2, part="evening")
    assert [s.strftime("%H:%M") for s, _ in evening] == ["17:00", "17:15"]


# ── reminder timing ──

SETTINGS = {
    "reminder_enabled": True, "reminder_hours_before": 24, "reminder_retry_minutes": 30,
    "reminder_window_start": datetime.time(9), "reminder_window_end": datetime.time(20),
}


def test_reminder_day_before_in_hours():
    assert reminders_schedule.first_reminder_at(_at(21, 11), _at(19, 12), SETTINGS, IST) == _at(20, 11)


def test_reminder_moved_into_calling_hours():
    # 08:00 appointment: 24h before is 08:00, before calling hours -> 09:00 that day.
    assert reminders_schedule.first_reminder_at(_at(21, 8), _at(19, 12), SETTINGS, IST) == _at(20, 9)
    # 21:00 appointment: 24h before is after hours -> 19:30 that day.
    assert reminders_schedule.first_reminder_at(_at(21, 21), _at(19, 12), SETTINGS, IST) == _at(20, 19, 30)


def test_reminder_for_late_booking_and_too_late():
    # Booked 6h ahead: call soon (in hours).
    assert reminders_schedule.first_reminder_at(_at(19, 18), _at(19, 12), SETTINGS, IST) == _at(19, 12, 5)
    # Booked 1h ahead: no reminder.
    assert reminders_schedule.first_reminder_at(_at(19, 13), _at(19, 12), SETTINGS, IST) is None
    assert reminders_schedule.first_reminder_at(_at(21, 11), _at(19, 12), {**SETTINGS, "reminder_enabled": False},
                                                IST) is None


def test_retry_respects_hours_and_appointment():
    assert reminders_schedule.retry_at(_at(19, 12), _at(21, 11), SETTINGS, IST) == _at(19, 12, 30)
    assert reminders_schedule.retry_at(_at(19, 19, 45), _at(21, 11), SETTINGS, IST) == _at(20, 9)
    assert reminders_schedule.retry_at(_at(19, 12), _at(19, 14), SETTINGS, IST) is None


# ── template-sized speech ──

def test_speech_parts_fit_toolexec_template_limit():
    from services.clinic.booking import SPEECH_PARTS, speech_parts

    text = " ".join(["अपॉइंटमेंट"] * 40)
    parts = speech_parts(text)
    assert len(parts) == SPEECH_PARTS and all(len(p) <= 115 for p in parts)
    assert speech_parts("Short sentence.") == ["Short sentence.", "", "", ""]
    long_text = "Just to confirm: Rahul Sharma with Dr. Anjali Mehta, Cardiology, tomorrow at 11:30 AM, " \
                "mobile number ending 3 2 1 0. The consultation fee is 1200 rupees. Shall I book it?"
    assert " ".join(p for p in speech_parts(long_text) if p) == long_text


def test_may_manage_caller_number_is_enough_spoken_is_second_factor():
    appt = {"patient_phone": "+919811100001", "booked_by_phone": "+919811100002"}
    assert booking.may_manage(appt, "+919811100002", None)
    assert booking.may_manage(appt, "+919811100001", None)
    assert booking.may_manage(appt, None, "9811100001")
    assert not booking.may_manage(appt, None, None)
    assert not booking.may_manage(appt, "+919811100003", None)
    # a spoken number counts against the patient's number only, never the booking phone
    assert not booking.may_manage(appt, None, "+919811100002")
    assert not booking.may_manage(appt, "+919811100003", "+919811100002")
    assert booking.may_manage(appt, "+919811100003", "+919811100001")
    assert not booking.may_manage({"patient_phone": "+919811100001", "booked_by_phone": None}, None, "garbage")
