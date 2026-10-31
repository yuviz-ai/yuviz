"""Booking lifecycle against a real database: availability, holds, the double-booking guard,
reschedule, cancel, who may manage a booking, and the outbox rows each change queues."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from libs.tenancy import tenant_conn
from services.clinic import booking

from .conftest import tomorrow_at

CALLER = "+919811100001"
OTHER = "+919811100002"


async def _prepare_and_book(run, *, slot, caller=CALLER, doctor="CARD-01", name="Rahul Sharma", language="en",
                            patient_phone=None):
    prep = await run(booking.prepare_booking, doctor=doctor, slot_start=slot, patient_name=name,
                     patient_phone=patient_phone, caller_phone=caller, language=language)
    if not prep["ok"]:
        return prep, None
    done = await run(booking.book, doctor_code=prep["doctor_code"], slot_start=prep["slot_start"],
                     patient_name=name, patient_phone=prep["patient_phone"], caller_phone=caller,
                     reason="chest pain on stairs", patient_age="54", language=language)
    return prep, done


async def test_availability_by_department_offers_slots_within_window(run):
    result = await run(booking.availability, doctor=None, department="cardiology", date=None, part_of_day=None,
                       language="en", holder_phone=CALLER)
    assert result["ok"]
    assert {d["doctor_code"] for d in result["doctors"]} == {"CARD-01", "CARD-02"}
    assert all(1 <= len(d["slots"]) <= 4 for d in result["doctors"])


async def test_availability_rejects_dates_outside_window(run):
    far = (datetime.date.today() + datetime.timedelta(days=40)).isoformat()
    result = await run(booking.availability, doctor="CARD-01", department=None, date=far, part_of_day=None,
                       language="en", holder_phone=CALLER)
    assert not result["ok"] and "only from" in result["message"]


async def test_prepare_is_deterministic_and_book_succeeds(run, pool, clinic_tenant):
    slot = tomorrow_at(10, 0)
    first = await run(booking.prepare_booking, doctor="CARD-01", slot_start=slot, patient_name="Rahul Sharma",
                      patient_phone=None, caller_phone=CALLER, language="en")
    again = await run(booking.prepare_booking, doctor="CARD-01", slot_start=slot, patient_name="Rahul Sharma",
                      patient_phone=None, caller_phone=CALLER, language="en")
    # toolexec matches the caller's "yes" by hashing these: they must not change between turns.
    assert {k: first[k] for k in ("doctor_code", "slot_start", "patient_phone")} == \
           {k: again[k] for k in ("doctor_code", "slot_start", "patient_phone")}
    assert "Shall I book it?" in first["confirm_text"] and "900 rupees" in first["confirm_text"]

    done = await run(booking.book, doctor_code=first["doctor_code"], slot_start=first["slot_start"],
                     patient_name="Rahul Sharma", patient_phone=first["patient_phone"], caller_phone=CALLER,
                     reason=None, patient_age=None, language="en")
    assert done["ok"] and len(done["reference"]) == 6
    kinds = await pool.fetch(
        "SELECT kind, payload->>'event' AS event FROM clinic_outbox o JOIN clinic_appointments a "
        "ON a.id = o.appointment_id WHERE a.reference = $1 AND o.tenant_id = $2", done["reference"],
        clinic_tenant["id"],
    )
    assert sorted((k["kind"], k["event"]) for k in kinds) == [("gcal_sync", None), ("sms", "booked")]


async def test_hold_blocks_another_caller(run):
    slot = tomorrow_at(10, 15)
    mine = await run(booking.prepare_booking, doctor="CARD-01", slot_start=slot, patient_name="A",
                     patient_phone=None, caller_phone=CALLER, language="en")
    theirs = await run(booking.prepare_booking, doctor="CARD-01", slot_start=slot, patient_name="B",
                       patient_phone=None, caller_phone=OTHER, language="en")
    assert mine["ok"] and not theirs["ok"]
    assert theirs["doctor_code"] == booking.NOTHING
    # And the held slot is not offered to the other caller.
    offered = await run(booking.availability, doctor="CARD-01", department=None, date=slot[:10],
                        part_of_day="morning", language="en", holder_phone=OTHER)
    assert all(s["slot_start"] != slot for s in offered["doctors"][0]["slots"])


async def test_cannot_double_book_a_slot(run):
    slot = tomorrow_at(11, 0)
    _, first = await _prepare_and_book(run, slot=slot, caller=CALLER, name="P One")
    assert first["ok"]
    prep, second = await _prepare_and_book(run, slot=slot, caller=OTHER, name="P Two")
    assert not prep["ok"] and second is None
    # Even skipping prepare (a stale read-back), book refuses.
    direct = await run(booking.book, doctor_code="CARD-01", slot_start=slot, patient_name="P Two",
                       patient_phone=OTHER, caller_phone=OTHER, reason=None, patient_age=None, language="en")
    assert not direct["ok"] and "just taken" in direct["spoken"]


async def test_concurrent_books_one_wins(pool, clinic_tenant):
    """Two books for the same slot racing in separate transactions: the exclusion constraint
    lets exactly one through."""
    slot = tomorrow_at(11, 30)

    async def attempt(phone: str, name: str):
        async with tenant_conn(pool, explicit_tenant=clinic_tenant["id"], reason="test") as conn:
            clinic = await booking.load_clinic(conn, clinic_tenant["id"])
            return await booking.book(conn, clinic, doctor_code="CARD-02", slot_start=slot, patient_name=name,
                                      patient_phone=phone, caller_phone=phone, reason=None, patient_age=None,
                                      language="en")

    results = await asyncio.gather(attempt(CALLER, "Racer A"), attempt(OTHER, "Racer B"))
    assert sorted(r["ok"] for r in results) == [False, True]


async def test_duplicate_booking_same_patient_same_doctor_refused(run):
    _, first = await _prepare_and_book(run, slot=tomorrow_at(12, 0), caller="+919811100003", name="Meena Gupta")
    assert first["ok"]
    prep, _ = await _prepare_and_book(run, slot=tomorrow_at(12, 30), caller="+919811100003", name="meena gupta")
    assert not prep["ok"] and "reschedule" in prep["confirm_text"]


async def test_leave_removes_slots(run, pool, clinic_tenant):
    slot = tomorrow_at(15, 0)
    start = datetime.datetime.fromisoformat(slot)
    await pool.execute(
        "INSERT INTO clinic_doctor_leaves (tenant_id, doctor_id, starts_at, ends_at, reason) "
        "VALUES ($1, $2, $3, $4, 'conference')",
        clinic_tenant["id"], clinic_tenant["doctors"]["CARD-02"], start, start + datetime.timedelta(hours=2),
    )
    prep = await run(booking.prepare_booking, doctor="CARD-02", slot_start=slot, patient_name="X",
                     patient_phone=None, caller_phone=CALLER, language="en")
    assert not prep["ok"] and "isn't available" in prep["confirm_text"]


async def test_find_reschedule_cancel_by_caller(run, pool):
    caller = "+919811100004"
    _, done = await _prepare_and_book(run, slot=tomorrow_at(13, 0), caller=caller, name="Asha Verma")
    ref = done["reference"]

    found = await run(booking.find_appointments, caller_phone=caller, reference=None, patient_phone=None,
                      language="en")
    assert [a["reference"] for a in found["appointments"]] == [ref]

    prep = await run(booking.prepare_reschedule, reference=ref, new_slot_start=tomorrow_at(14, 0),
                     caller_phone=caller, patient_phone=None, language="en")
    assert prep["ok"] and "move" in prep["confirm_text"]
    moved = await run(booking.reschedule, reference=prep["reference"], new_slot_start=prep["new_slot_start"],
                      caller_phone=caller, patient_phone=None, language="en")
    assert moved["ok"] and moved["slot_start"] == tomorrow_at(14, 0)
    # The old slot is free again.
    again = await run(booking.prepare_booking, doctor="CARD-01", slot_start=tomorrow_at(13, 0), patient_name="Z",
                      patient_phone=None, caller_phone=OTHER, language="en")
    assert again["ok"]

    prep_cancel = await run(booking.prepare_cancel, reference=ref, caller_phone=caller, patient_phone=None,
                            language="en")
    assert prep_cancel["ok"]
    cancelled = await run(booking.cancel, reference=ref, caller_phone=caller, patient_phone=None, reason="better",
                          language="en")
    assert cancelled["ok"]
    events = await pool.fetch(
        "SELECT payload->>'event' AS e FROM clinic_outbox o JOIN clinic_appointments a ON a.id = o.appointment_id "
        "WHERE a.reference = $1 AND o.kind = 'sms' ORDER BY o.created_at", ref,
    )
    assert [e["e"] for e in events] == ["booked", "rescheduled", "cancelled"]
    gcal = await pool.fetchrow(
        "SELECT o.rev, o.status FROM clinic_outbox o JOIN clinic_appointments a ON a.id = o.appointment_id "
        "WHERE a.reference = $1 AND o.kind = 'gcal_sync'", ref,
    )
    assert gcal["rev"] == 2 and gcal["status"] == "pending"   # one row, re-armed by each change


async def test_strangers_cannot_see_or_change_a_booking(run):
    _, done = await _prepare_and_book(run, slot=tomorrow_at(16, 0), caller="+919811100005", name="Owner")
    ref = done["reference"]
    stranger = "+919899999999"
    found = await run(booking.find_appointments, caller_phone=stranger, reference=ref, patient_phone=None,
                      language="en")
    assert found["appointments"] == []
    prep = await run(booking.prepare_cancel, reference=ref, caller_phone=stranger, patient_phone=None, language="en")
    assert not prep["ok"]
    cancelled = await run(booking.cancel, reference=ref, caller_phone=stranger, patient_phone=None, reason=None,
                          language="en")
    assert not cancelled["ok"]
    # Family member calling from another phone who knows the patient's number may.
    allowed = await run(booking.prepare_cancel, reference=ref, caller_phone=stranger,
                        patient_phone="9811100005", language="en")
    assert allowed["ok"]


async def test_spoken_number_alone_lists_nothing(run):
    """A stranger who merely says the patient's number (no booking number) must get no appointments."""
    _, done = await _prepare_and_book(run, slot=tomorrow_at(10, 30), caller="+919811100015", name="Private Patient")
    stranger = "+919899999998"
    for said in ("9811100015", "+919811100015", "09811100015"):
        found = await run(booking.find_appointments, caller_phone=stranger, reference=None, patient_phone=said,
                          language="en")
        assert found["appointments"] == []
    found = await run(booking.find_appointments, caller_phone=None, reference=None, patient_phone="9811100015",
                      language="en")
    assert found["appointments"] == []
    # The call's own number still lists it.
    own = await run(booking.find_appointments, caller_phone="+919811100015", reference=None, patient_phone=None,
                    language="en")
    assert [a["reference"] for a in own["appointments"]] == [done["reference"]]
    # With the booking number plus the patient's number it works, from another phone.
    with_ref = await run(booking.find_appointments, caller_phone=stranger, reference=done["reference"],
                         patient_phone="9811100015", language="en")
    assert [a["reference"] for a in with_ref["appointments"]] == [done["reference"]]


async def test_spoken_booking_phone_does_not_authorize(run):
    """Booked on behalf of a patient: saying the booker's number is not a second factor (only the patient's is)."""
    prep, done = await _prepare_and_book(run, slot=tomorrow_at(11, 30), caller="+919811100016",
                                         patient_phone="+919811100017", name="Meera Rao")
    ref = done["reference"]
    stranger = "+919899999997"
    nope = await run(booking.find_appointments, caller_phone=stranger, reference=ref, patient_phone="9811100016",
                     language="en")
    assert nope["appointments"] == []
    assert not (await run(booking.prepare_cancel, reference=ref, caller_phone=stranger,
                          patient_phone="9811100016", language="en"))["ok"]
    # The booker's real call number still works, and lists the booking without a reference.
    listed = await run(booking.find_appointments, caller_phone="+919811100016", reference=None, patient_phone=None,
                       language="en")
    assert [a["reference"] for a in listed["appointments"]] == [ref]


async def test_hindi_read_back_and_confirmation(run):
    prep, done = await _prepare_and_book(run, slot=tomorrow_at(9, 30), caller="+919811100006", name="सुनीता",
                                         language="hi", doctor="CARD-02")
    assert "क्या मैं बुक कर दूँ?" in prep["confirm_text"] and "कल सुबह 9:30 बजे" in prep["confirm_text"]
    assert done["ok"] and "बुकिंग नंबर" in done["spoken"]


async def test_reminder_context_and_outcome(run, pool):
    caller = "+919811100007"
    _, done = await _prepare_and_book(run, slot=tomorrow_at(16, 30), caller=caller, name="Rekha")
    none_yet = await run(booking.reminder_context, caller_phone=caller, language="en")
    assert not none_yet["ok"]
    await pool.execute("UPDATE clinic_appointments SET reminder_status = 'calling' WHERE reference = $1",
                       done["reference"])
    ctx = await run(booking.reminder_context, caller_phone=caller, language="en")
    assert ctx["ok"] and ctx["appointment"]["reference"] == done["reference"]
    out = await run(booking.record_reminder_outcome, reference=done["reference"], outcome="confirmed",
                    caller_phone=caller, language="en")
    assert out["ok"]
    assert await pool.fetchval("SELECT reminder_status FROM clinic_appointments WHERE reference = $1",
                               done["reference"]) == "confirmed"


async def test_day_words_resolve_server_side(run):
    def check(conn, clinic):
        today = clinic.today
        wanted_thursday = today + datetime.timedelta(days=(3 - today.weekday()) % 7)
        return {
            "thursday": booking.parse_day("Thursday", clinic) == wanted_thursday,
            "गुरुवार": booking.parse_day("गुरुवार", clinic) == wanted_thursday,
            "next thursday": booking.parse_day("next thursday", clinic) == wanted_thursday,
            "कल": booking.parse_day("कल", clinic) == today + datetime.timedelta(days=1),
            "परसों": booking.parse_day("परसों", clinic) == today + datetime.timedelta(days=2),
            "iso": booking.parse_day("2026-10-22", clinic) == datetime.date(2026, 10, 22),
        }

    async def wrapped(conn, clinic):
        return check(conn, clinic)

    assert all((await run(wrapped)).values())
    result = await run(booking.availability, doctor="CARD-01", department=None, date="tomorrow", part_of_day=None,
                       language="hi", holder_phone=None)
    assert result["requested_day_spoken"] == "कल"
    bad = await run(booking.availability, doctor="CARD-01", department=None, date="someday", part_of_day=None,
                    language="en", holder_phone=None)
    assert not bad["ok"] and "weekday" in bad["message"]


async def test_reschedule_to_another_doctor_keeps_booking_number(run, pool):
    caller = "+919811100008"
    _, done = await _prepare_and_book(run, slot=tomorrow_at(13, 30), caller=caller, name="Vivek", doctor="CARD-01")
    ref = done["reference"]
    prep = await run(booking.prepare_reschedule, reference=ref, new_slot_start=tomorrow_at(14, 30),
                     caller_phone=caller, patient_phone=None, language="en", doctor="CARD-02")
    assert prep["ok"] and prep["doctor_code"] == "CARD-02" and "stays the same" in prep["confirm_text"]
    moved = await run(booking.reschedule, reference=prep["reference"], new_slot_start=prep["new_slot_start"],
                      caller_phone=caller, patient_phone=None, language="en", doctor_code=prep["doctor_code"])
    assert moved["ok"] and moved["reference"] == ref and "Vikram Rao" in moved["spoken"]
    row = await pool.fetchrow(
        "SELECT d.code, a.status FROM clinic_appointments a JOIN clinic_doctors d ON d.id = a.doctor_id "
        "WHERE a.reference = $1", ref)
    assert (row["code"], row["status"]) == ("CARD-02", "booked")
    # Same doctor when none is named; the refusal sentinel never moves anything.
    same = await run(booking.prepare_reschedule, reference=ref, new_slot_start=tomorrow_at(15, 0),
                     caller_phone=caller, patient_phone=None, language="en")
    assert same["doctor_code"] == "CARD-02"
    refused = await run(booking.reschedule, reference=ref, new_slot_start=tomorrow_at(15, 0), caller_phone=caller,
                        patient_phone=None, language="en", doctor_code=booking.NOTHING)
    assert not refused["ok"]


@pytest.mark.parametrize("name", ["Patient", "the patient", "मरीज़", "Caller.", "  ", "N/A"])
async def test_placeholder_names_are_refused(run, name):
    prep = await run(booking.prepare_booking, doctor="CARD-01", slot_start=tomorrow_at(16, 15), patient_name=name,
                     patient_phone=None, caller_phone="+919811100009", language="en")
    assert not prep["ok"] and prep["confirm_text"] == "What is the patient's full name?"
    done = await run(booking.book, doctor_code="CARD-01", slot_start=tomorrow_at(16, 15), patient_name=name,
                     patient_phone="+919811100009", caller_phone="+919811100009", reason=None, patient_age=None,
                     language="hi")
    assert not done["ok"] and done["spoken"] == "मरीज़ का पूरा नाम क्या है?"


def test_real_names_are_not_placeholders():
    for name in ("Rahul Sharma", "Patil", "Mr. Verma", "चंदन कुमार", "Testa"):
        assert not booking.is_placeholder_name(name), name


async def test_availability_comes_with_a_sentence_to_speak(run):
    result = await run(booking.availability, doctor=None, department="cardiology", date="tomorrow", part_of_day=None,
                       language="en", holder_phone=None)
    assert result["spoken"].startswith("Dr. ") and "tomorrow at " in result["spoken"]
    assert result["spoken"].endswith("are available. Which time suits you?")
    assert result["spoken"].count("tomorrow") == 2                # once per doctor, not per time
    one = await run(booking.availability, doctor="CARD-02", department=None, date="tomorrow", part_of_day=None,
                    language="hi", holder_phone=None)
    assert one["spoken"].startswith("Dr. Vikram Rao कल ") or one["spoken"].startswith("Dr. Vikram Rao")
    assert one["spoken"].count(" या ") == 2 and one["spoken"].endswith("आपके लिए कौन सा समय ठीक रहेगा?")


async def test_no_sentence_when_there_is_nothing_to_offer(run):
    far = await run(booking.availability, doctor="CARD-01", department=None, date="2099-01-01", part_of_day=None,
                    language="en", holder_phone=None)
    nobody = await run(booking.availability, doctor="Dr. Nobody Here", department=None, date=None, part_of_day=None,
                       language="en", holder_phone=None)
    assert "spoken" not in far and "spoken" not in nobody          # the model handles these
