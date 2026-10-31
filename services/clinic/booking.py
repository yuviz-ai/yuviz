"""Availability and the booking lifecycle, behind the agent's tools.

Every change is two calls. `prepare_*` checks the request against the database, holds the
slot for this caller and returns the read-back sentence; toolexec speaks it and waits for
the caller's yes (its confirmation gate). The action call then re-checks everything and
commits. What a prepare returns is deterministic for the same request, because toolexec
hashes it to match the "yes" to the read-back.

Results are always HTTP 200 with `ok` and a `spoken` / `confirm_text` sentence: toolexec
gives the model only an error code for a non-2xx answer, and the caller deserves the reason.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from . import notifications, phones, reminders_schedule, slots, speech

log = logging.getLogger(__name__)

# Sentinel the prepare step returns when nothing may be booked; the action step refuses it.
NOTHING = "-"
# Toolexec cuts every template value at 120 characters (a guard on model-supplied text), so a
# sentence meant for a template goes out as up to SPEECH_PARTS chunks: `confirm_text_1`.. etc.
SPEECH_PARTS = 4
_PART_CHARS = 115


def speech_parts(text: str) -> list[str]:
    """Split on word boundaries into exactly SPEECH_PARTS chunks of at most _PART_CHARS each
    (trailing ones empty). Text too long to fit is cut, never a chunk over the limit."""
    parts, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= _PART_CHARS:
            current = candidate
            continue
        parts.append(current)
        current = word[:_PART_CHARS]
    parts.append(current)
    parts = parts[:SPEECH_PARTS]
    return parts + [""] * (SPEECH_PARTS - len(parts))


def with_parts(result: dict, key: str) -> dict:
    """Adds key_1..key_N to a tool result for its read-back or success template."""
    for index, part in enumerate(speech_parts(result.get(key, "")), start=1):
        result[f"{key}_{index}"] = part
    return result
_MAX_DOCTORS_OFFERED = 3


class ClinicNotConfigured(LookupError):
    pass


@dataclass(frozen=True)
class Clinic:
    tenant_id: str
    settings: dict
    tz: ZoneInfo
    now: datetime.datetime          # in the clinic's timezone

    @property
    def today(self) -> datetime.date:
        return self.now.date()

    @property
    def last_bookable_day(self) -> datetime.date:
        return self.today + datetime.timedelta(days=self.settings["booking_window_days"] - 1)

    @property
    def not_before(self) -> datetime.datetime:
        return self.now + datetime.timedelta(minutes=self.settings["min_lead_minutes"])

    def local(self, moment: datetime.datetime) -> datetime.datetime:
        return moment.astimezone(self.tz)

    def when(self, moment: datetime.datetime, language: str) -> str:
        return speech.spoken_when(self.local(moment), self.today, language)


async def load_clinic(conn: Any, tenant_id: str, *, now: datetime.datetime | None = None) -> Clinic:
    row = await conn.fetchrow("SELECT * FROM clinic_settings WHERE tenant_id = $1", tenant_id)
    if row is None:
        raise ClinicNotConfigured("clinic is not set up for this tenant")
    tz = ZoneInfo(row["timezone"])
    current = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
    return Clinic(tenant_id=tenant_id, settings=dict(row), tz=tz, now=current)


# ── lookups ───────────────────────────────────────────────────────────────

_DOCTOR_TITLE_RE = re.compile(r"^\s*(dr\.?|doctor|डॉ\.?|डॉक्टर)\s+", re.IGNORECASE)


def _clean_name(text: str) -> str:
    return _DOCTOR_TITLE_RE.sub("", text or "").strip()


async def list_departments(conn: Any, clinic: Clinic) -> list[dict]:
    rows = await conn.fetch(
        "SELECT d.*, (SELECT count(*) FROM clinic_doctors x "
        "             WHERE x.department_id = d.id AND x.active AND x.deleted_at IS NULL) AS doctor_count "
        "FROM clinic_departments d WHERE d.tenant_id = $1 AND d.deleted_at IS NULL "
        "ORDER BY d.sort_order, d.name",
        clinic.tenant_id,
    )
    return [dict(r) for r in rows]


async def find_department(conn: Any, clinic: Clinic, text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    row = await conn.fetchrow(
        "SELECT * FROM clinic_departments WHERE tenant_id = $1 AND deleted_at IS NULL "
        "AND (lower(code) = lower($2) OR lower(name) = lower($2) OR name_hi = $2) LIMIT 1",
        clinic.tenant_id, text,
    )
    if row is None:
        row = await conn.fetchrow(
            "SELECT * FROM clinic_departments WHERE tenant_id = $1 AND deleted_at IS NULL "
            "AND (name ILIKE '%' || $2 || '%' OR $2 ILIKE '%' || name || '%' OR name_hi = $2) "
            "ORDER BY sort_order LIMIT 1",
            clinic.tenant_id, text,
        )
    return dict(row) if row else None


_DOCTOR_SELECT = (
    "SELECT doc.*, dep.name AS department_name, dep.name_hi AS department_name_hi, "
    "       dep.code AS department_code, dep.location AS department_location "
    "FROM clinic_doctors doc JOIN clinic_departments dep ON dep.id = doc.department_id "
    "WHERE doc.tenant_id = $1 AND doc.deleted_at IS NULL AND doc.active AND dep.deleted_at IS NULL "
)


async def find_doctors(
    conn: Any, clinic: Clinic, *, doctor: str | None = None, department_id: Any = None,
    language: str | None = None,
) -> list[dict]:
    """By code (exact), else by any part of the name; optionally within one department
    and only doctors who consult in `language`."""
    where, args = [], [clinic.tenant_id]
    if doctor:
        exact = await conn.fetch(_DOCTOR_SELECT + "AND lower(doc.code) = lower($2)", clinic.tenant_id, doctor.strip())
        if exact:
            return [dict(r) for r in exact]
        name = _clean_name(doctor)
        args.append(name)
        where.append(f"(doc.name ILIKE '%' || ${len(args)} || '%' OR doc.name_hi ILIKE '%' || ${len(args)} || '%')")
    if department_id is not None:
        args.append(department_id)
        where.append(f"doc.department_id = ${len(args)}")
    if language in speech.LANGUAGES:
        args.append(language)
        where.append(f"${len(args)} = ANY(doc.languages)")
    sql = _DOCTOR_SELECT + "".join(f"AND {w} " for w in where) + "ORDER BY dep.sort_order, doc.name"
    return [dict(r) for r in await conn.fetch(sql, *args)]


async def _doctor_by_id(conn: Any, clinic: Clinic, doctor_id: Any) -> dict | None:
    row = await conn.fetchrow(_DOCTOR_SELECT + "AND doc.id = $2", clinic.tenant_id, doctor_id)
    return dict(row) if row else None


def _department_of(doctor: dict) -> dict:
    return {"name": doctor["department_name"], "name_hi": doctor["department_name_hi"]}


# ── availability ─────────────────────────────────────────────────────────

async def _schedule_blocks(conn: Any, doctor_id: Any) -> list[slots.ScheduleBlock]:
    rows = await conn.fetch(
        "SELECT weekday, start_time, end_time, valid_from, valid_to FROM clinic_doctor_schedules "
        "WHERE doctor_id = $1", doctor_id,
    )
    return [slots.ScheduleBlock(r["weekday"], r["start_time"], r["end_time"], r["valid_from"], r["valid_to"])
            for r in rows]


async def _unavailable(
    conn: Any, clinic: Clinic, doctor_id: Any, start: datetime.datetime, end: datetime.datetime,
    *, holder_phone: str | None, exclude_appointment_id: Any = None,
) -> list[slots.Interval]:
    """Leave, live bookings (except the one being moved) and other callers' unexpired holds."""
    rows = await conn.fetch(
        "SELECT starts_at, ends_at FROM clinic_doctor_leaves "
        " WHERE doctor_id = $1 AND starts_at < $3 AND ends_at > $2 "
        "UNION ALL "
        "SELECT starts_at, ends_at FROM clinic_appointments "
        " WHERE doctor_id = $1 AND status = 'booked' AND starts_at < $3 AND ends_at > $2 "
        "   AND ($4::uuid IS NULL OR id <> $4::uuid) "
        "UNION ALL "
        "SELECT h.starts_at, h.starts_at + make_interval(mins => d.slot_minutes) "
        "  FROM clinic_slot_holds h JOIN clinic_doctors d ON d.id = h.doctor_id "
        " WHERE h.doctor_id = $1 AND h.expires_at > now() AND h.holder_phone IS DISTINCT FROM $5 "
        "   AND h.starts_at < $3 AND h.starts_at >= $2 - interval '1 day'",
        doctor_id, start, end, exclude_appointment_id, holder_phone,
    )
    return [(r["starts_at"], r["ends_at"]) for r in rows]


async def doctor_free_slots(
    conn: Any, clinic: Clinic, doctor: dict, first_day: datetime.date, last_day: datetime.date,
    *, holder_phone: str | None = None, exclude_appointment_id: Any = None,
) -> list[slots.Interval]:
    first_day = max(first_day, clinic.today)
    last_day = min(last_day, clinic.last_bookable_day)
    if first_day > last_day:
        return []
    range_start = datetime.datetime.combine(first_day, datetime.time.min, tzinfo=clinic.tz)
    range_end = datetime.datetime.combine(last_day + datetime.timedelta(days=1), datetime.time.min, tzinfo=clinic.tz)
    return slots.free_slots(
        blocks=await _schedule_blocks(conn, doctor["id"]),
        slot_minutes=doctor["slot_minutes"], tz=clinic.tz,
        first_day=first_day, last_day=last_day, not_before=clinic.not_before,
        unavailable=await _unavailable(
            conn, clinic, doctor["id"], range_start, range_end,
            holder_phone=holder_phone, exclude_appointment_id=exclude_appointment_id,
        ),
    )


def _slot_view(clinic: Clinic, interval: slots.Interval, language: str) -> dict:
    start = clinic.local(interval[0])
    return {"slot_start": start.isoformat(), "spoken": clinic.when(start, language)}


def doctor_view(doctor: dict, language: str) -> dict:
    return {
        "doctor_code": doctor["code"],
        "name": speech.doctor_name(doctor, language),
        "department": speech.department_name(_department_of(doctor), language),
        "department_code": doctor["department_code"],
        "qualifications": doctor["qualifications"],
        "specialization": doctor["specialization"],
        "experience_years": doctor["experience_years"],
        "languages": list(doctor["languages"]),
        "consultation_fee_inr": doctor["consultation_fee"],
        "gender": doctor["gender"],
        "location": doctor["department_location"],
        "about": doctor["bio"],
    }


_DAY_OFFSETS = {"today": 0, "aaj": 0, "आज": 0, "tomorrow": 1, "kal": 1, "कल": 1,
                "day after tomorrow": 2, "parson": 2, "parso": 2, "परसों": 2}
_WEEKDAYS = {
    **{name: i for i, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                         "sunday"))},
    **{name: i for i, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))},
    **{name: i for i, name in enumerate(speech._HI_WEEKDAYS)},
    **{name: i for i, name in enumerate(("somvar", "mangalvar", "budhvar", "guruvar", "shukravar", "shanivar",
                                         "ravivar"))},
}


def parse_day(text: str | None, clinic: Clinic) -> datetime.date | None:
    """YYYY-MM-DD, or the caller's own words: today/tomorrow/कल/परसों, or a weekday name in
    English or Hindi (the next such day, today included). Resolved here, in the clinic's
    timezone, because models get "Thursday" wrong surprisingly often."""
    if not text:
        return None
    raw = str(text).strip()
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        pass
    words = raw.casefold().replace("next ", "").replace("this ", "").strip(" .,?")
    if words in _DAY_OFFSETS:
        return clinic.today + datetime.timedelta(days=_DAY_OFFSETS[words])
    if words in _WEEKDAYS:
        return clinic.today + datetime.timedelta(days=(_WEEKDAYS[words] - clinic.today.weekday()) % 7)
    raise ValueError("date must be YYYY-MM-DD, a weekday name, today or tomorrow")


def parse_slot(text: str | None, clinic: Clinic) -> datetime.datetime | None:
    """ISO 8601; a time without an offset is the clinic's local time."""
    if not text:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=clinic.tz) if moment.tzinfo is None else moment.astimezone(clinic.tz)


async def availability(
    conn: Any, clinic: Clinic, *, doctor: str | None, department: str | None, date: str | None,
    part_of_day: str | None, language: str, holder_phone: str | None,
) -> dict:
    language = speech.lang_or_default(language)
    part = part_of_day if part_of_day in ("morning", "afternoon", "evening") else None
    try:
        day = parse_day(date, clinic)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    if day is not None and not (clinic.today <= day <= clinic.last_bookable_day):
        return {
            "ok": False,
            "message": (f"Bookings are open only from {clinic.today.isoformat()} to "
                        f"{clinic.last_bookable_day.isoformat()}. Ask for a date in that range."),
        }

    if doctor:
        candidates = await find_doctors(conn, clinic, doctor=doctor)
        if not candidates:
            return {"ok": False, "message": f"No doctor matches {doctor!r}. Use find_doctors to look them up."}
        if len(candidates) > 1:
            return {
                "ok": False, "message": "More than one doctor matches; ask the caller which one.",
                "matches": [doctor_view(d, language) for d in candidates[:5]],
            }
    elif department:
        dept = await find_department(conn, clinic, department)
        if dept is None:
            return {"ok": False, "message": f"No department matches {department!r}. Use list_departments."}
        candidates = await find_doctors(conn, clinic, department_id=dept["id"])
        if not candidates:
            return {"ok": False, "message": "That department has no doctor taking appointments."}
    else:
        return {"ok": False, "message": "Say which doctor or department to check."}

    first_day = day or clinic.today
    last_day = day or clinic.last_bookable_day
    offers = []
    for candidate in candidates:
        free = await doctor_free_slots(conn, clinic, candidate, first_day, last_day, holder_phone=holder_phone)
        chosen = slots.pick_for_offer(free, limit=6 if day else 4, part=part)
        entry = {**doctor_view(candidate, language), "slots": [_slot_view(clinic, s, language) for s in chosen]}
        if not chosen:
            # Nothing on the asked day: name the next day that has room.
            later = await doctor_free_slots(conn, clinic, candidate, last_day + datetime.timedelta(days=1),
                                            clinic.last_bookable_day, holder_phone=holder_phone)
            if later:
                entry["next_available"] = _slot_view(clinic, later[0], language)
        offers.append((chosen[0][0] if chosen else None, entry))
    offers.sort(key=lambda o: (o[0] is None, o[0] or clinic.now))
    doctors = [entry for _, entry in offers[:_MAX_DOCTORS_OFFERED]]
    any_slots = any(d["slots"] for d in doctors)
    spoken = _offer_sentence(clinic, doctors, language, single_doctor=bool(doctor)) if any_slots else None
    return {
        **({"spoken": spoken} if spoken else {}),
        "ok": True,
        "today": clinic.today.isoformat(),
        **({"requested_day": day.isoformat(), "requested_day_spoken": speech.spoken_date(day, clinic.today, language)}
           if day else {}),
        "doctors": doctors,
        "message": (
            "Offer two or three of these times, using the 'spoken' wording, and pass the chosen "
            "slot_start unchanged to clinic_book_appointment."
            if any_slots else
            f"No free slots for that request up to {clinic.last_bookable_day.isoformat()}. "
            "Offer next_available if present, or another doctor in the department."
        ),
    }


def _times_phrase(clinic: Clinic, starts: list[datetime.datetime], language: str) -> str:
    """'tomorrow at 5 PM or 5:15 PM' — the day said once when the times share it."""
    joiner = " या " if language == "hi" else " or "
    if len({s.date() for s in starts}) == 1:
        day = speech.spoken_date(starts[0].date(), clinic.today, language)
        times = joiner.join(speech.spoken_time(s, language) for s in starts)
        return f"{day} {times}" if language == "hi" else f"{day} at {times}"
    return joiner.join(clinic.when(s, language) for s in starts)


def _offer_sentence(clinic: Clinic, doctors: list[dict], language: str, *, single_doctor: bool) -> str:
    """The options read straight to the caller (the tool's success template), so a tool turn
    needs no second model call: up to two doctors with two times each, or three for one doctor."""
    per_doctor = 3 if single_doctor else 2
    parts = []
    for entry in [d for d in doctors if d["slots"]][: 1 if single_doctor else 2]:
        starts = [datetime.datetime.fromisoformat(s["slot_start"]) for s in entry["slots"][:per_doctor]]
        parts.append((entry["name"], _times_phrase(clinic, starts, language)))
    if language == "hi":
        offers = " और ".join(f"{name} {times}" for name, times in parts)
        return f"{offers} उपलब्ध हैं। आपके लिए कौन सा समय ठीक रहेगा?"
    offers = ", and ".join(f"{name} {times}" for name, times in parts)
    return f"{offers} {'is' if len(parts) == 1 else 'are'} available. Which time suits you?"


# ── shared checks ────────────────────────────────────────────────────────

async def _hold_slot(conn: Any, clinic: Clinic, doctor_id: Any, start: datetime.datetime, holder: str) -> bool:
    """Hold for this caller; False when someone else holds it and their hold has not expired."""
    row = await conn.fetchrow(
        "INSERT INTO clinic_slot_holds (tenant_id, doctor_id, starts_at, holder_phone, expires_at) "
        "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5)) "
        "ON CONFLICT (doctor_id, starts_at) DO UPDATE "
        "SET holder_phone = EXCLUDED.holder_phone, expires_at = EXCLUDED.expires_at "
        "WHERE clinic_slot_holds.expires_at <= now() OR clinic_slot_holds.holder_phone = EXCLUDED.holder_phone "
        "RETURNING 1",
        clinic.tenant_id, doctor_id, start, holder, clinic.settings["hold_seconds"],
    )
    return row is not None


async def _slot_is_bookable(
    conn: Any, clinic: Clinic, doctor: dict, start: datetime.datetime, *, holder: str | None,
    exclude_appointment_id: Any = None,
) -> bool:
    free = await doctor_free_slots(
        conn, clinic, doctor, start.date(), start.date(),
        holder_phone=holder, exclude_appointment_id=exclude_appointment_id,
    )
    return any(s == start for s, _ in free)


async def _alternatives_sentence(
    conn: Any, clinic: Clinic, doctor: dict, around: datetime.datetime | None, language: str, holder: str | None,
    exclude_appointment_id: Any = None,
) -> str:
    first = around.date() if around and around.date() >= clinic.today else clinic.today
    free = await doctor_free_slots(conn, clinic, doctor, first, clinic.last_bookable_day,
                                   holder_phone=holder, exclude_appointment_id=exclude_appointment_id)
    chosen = slots.pick_for_offer(free, limit=3)
    name = speech.doctor_name(doctor, language)
    if not chosen:
        return (f"{name} के पास अगले {clinic.settings['booking_window_days']} दिनों में कोई समय खाली नहीं है।"
                if language == "hi" else
                f"{name} has no free time in the next {clinic.settings['booking_window_days']} days.")
    times = ", ".join(clinic.when(s, language) for s, _ in chosen)
    return (f"{name} के पास ये समय खाली हैं: {times}। आपके लिए कौन सा ठीक रहेगा?"
            if language == "hi" else
            f"{name} is free {times}. Which would suit you?")


def _refusal(confirm_text: str, **fields: str) -> dict:
    return {"ok": False, "confirm_text": confirm_text, **{k: NOTHING for k in fields}}


async def _appointment_by_reference(conn: Any, clinic: Clinic, reference: str | None) -> dict | None:
    if not reference:
        return None
    digits = re.sub(r"\D", "", str(reference))
    row = await conn.fetchrow(
        "SELECT * FROM clinic_appointments WHERE tenant_id = $1 AND reference = $2",
        clinic.tenant_id, digits,
    )
    return dict(row) if row else None


def may_manage(appointment: dict, caller_phone: str | None, claimed_phone: str | None = None) -> bool:
    """Who may see or change an appointment, once the caller has named it by booking number.

    The server-side number of the call (`caller_phone`, from call metadata) is enough on its own when it is the
    patient's number or the number the booking was made from. A number the caller merely SAYS (`claimed_phone`,
    model-supplied) is the second factor for someone calling from another phone: it counts only with the booking
    number in hand, and only against the patient's own number, never the booking phone."""
    caller = phones.normalize_phone(caller_phone)
    if caller is not None and caller in (appointment["patient_phone"], appointment.get("booked_by_phone")):
        return True
    claimed = phones.normalize_phone(claimed_phone)
    return claimed is not None and claimed == appointment["patient_phone"]


def _not_found_sentence(language: str) -> str:
    return ("इस नंबर से मुझे कोई बुकिंग नहीं मिली। कृपया बुकिंग नंबर और बुकिंग के समय दिया गया मोबाइल नंबर बताइए।"
            if language == "hi" else
            "I couldn't find that booking for this phone number. Could you tell me the booking number "
            "and the mobile number it was made with?")


def _fee_sentence(doctor: dict, language: str) -> str:
    fee = doctor.get("consultation_fee")
    if not fee:
        return ""
    return f" परामर्श शुल्क {fee} रुपये है।" if language == "hi" else f" The consultation fee is {fee} rupees."


# ── book ─────────────────────────────────────────────────────────────────

async def prepare_booking(
    conn: Any, clinic: Clinic, *, doctor: str, slot_start: str, patient_name: str,
    patient_phone: str | None, caller_phone: str | None, language: str,
) -> dict:
    language = speech.lang_or_default(language)
    nothing = {"doctor_code": "", "slot_start": "", "patient_phone": ""}
    caller = phones.normalize_phone(caller_phone)
    patient = phones.normalize_phone(patient_phone) or caller
    name = (patient_name or "").strip()
    if is_placeholder_name(name):
        return _refusal(_ask_name(language), **nothing)
    if patient is None:
        return _refusal("बुकिंग के लिए 10 अंकों का मोबाइल नंबर बताइए।" if language == "hi"
                        else "Could you give me a 10-digit mobile number for the booking?", **nothing)

    matches = await find_doctors(conn, clinic, doctor=doctor)
    if len(matches) != 1:
        return _refusal("आप किस डॉक्टर से मिलना चाहेंगे?" if language == "hi"
                        else "Which doctor would you like to see?", **nothing)
    doc = matches[0]
    start = parse_slot(slot_start, clinic)
    if start is None or not await _slot_is_bookable(conn, clinic, doc, start, holder=caller or patient):
        sorry = "माफ़ कीजिए, वह समय उपलब्ध नहीं है। " if language == "hi" else "Sorry, that time isn't available. "
        return _refusal(sorry + await _alternatives_sentence(conn, clinic, doc, start, language, caller or patient),
                        **nothing)

    active = await conn.fetch(
        "SELECT a.*, d.name AS doctor_name, d.name_hi AS doctor_name_hi FROM clinic_appointments a "
        "JOIN clinic_doctors d ON d.id = a.doctor_id "
        "WHERE a.tenant_id = $1 AND a.status = 'booked' AND a.starts_at > now() AND a.patient_phone = $2",
        clinic.tenant_id, patient,
    )
    same = [a for a in active if a["doctor_id"] == doc["id"] and a["patient_name"].casefold() == name.casefold()]
    if same:
        when = clinic.when(same[0]["starts_at"], language)
        return _refusal(
            f"{name} का {speech.doctor_name(doc, language)} के साथ {when} पहले से अपॉइंटमेंट है। क्या आप उसका समय बदलना चाहेंगे?"
            if language == "hi" else
            f"{name} already has an appointment with {speech.doctor_name(doc, language)} {when}. "
            "Would you like to reschedule that one instead?",
            **nothing,
        )
    if len(active) >= clinic.settings["max_active_per_phone"]:
        return _refusal(
            "इस मोबाइल नंबर पर पहले से कई अपॉइंटमेंट हैं। नई बुकिंग के लिए कृपया रिसेप्शन से संपर्क करें।"
            if language == "hi" else
            "This mobile number already has several upcoming appointments. "
            "For another one, please contact the front desk.",
            **nothing,
        )

    if not await _hold_slot(conn, clinic, doc["id"], start, caller or patient):
        sorry = "माफ़ कीजिए, यह समय अभी कोई और बुक कर रहा है। " if language == "hi" else "Sorry, someone else is booking that time right now. "
        return _refusal(sorry + await _alternatives_sentence(conn, clinic, doc, start, language, caller or patient),
                        **nothing)

    when = clinic.when(start, language)
    dept = speech.department_name(_department_of(doc), language)
    digits = phones.spoken_last_digits(patient)
    if language == "hi":
        text = (f"पुष्टि कर लूँ: {name} के लिए {speech.doctor_name(doc, language)}, {dept}, के साथ {when}, "
                f"मोबाइल नंबर के आख़िरी अंक {digits}।{_fee_sentence(doc, language)} क्या मैं बुक कर दूँ?")
    else:
        text = (f"Just to confirm: {name} with {speech.doctor_name(doc, language)}, {dept}, {when}, "
                f"mobile number ending {digits}.{_fee_sentence(doc, language)} Shall I book it?")
    return {
        "ok": True, "doctor_code": doc["code"], "slot_start": clinic.local(start).isoformat(),
        "patient_phone": patient, "confirm_text": text,
    }


# Words a model fills in when it never asked for the name. A booking under "Patient" is
# useless to the front desk, so these are refused and the caller is asked instead.
_PLACEHOLDER_NAMES = frozenset({
    "patient", "the patient", "caller", "customer", "user", "unknown", "name", "patient name",
    "full name", "na", "n a", "none", "test", "sir", "madam", "mr", "mrs", "ms",
    "मरीज़", "मरीज", "रोगी", "ग्राहक", "नाम",
})


def _name_key(text: str) -> str:
    """Case, punctuation and nukta/diacritic spelling variants folded away (ज़ = ज + ़)."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^\w\s]", " ", stripped).split())


_PLACEHOLDER_KEYS = frozenset(_name_key(n) for n in _PLACEHOLDER_NAMES)


def is_placeholder_name(name: str) -> bool:
    key = _name_key(name)
    return not key or key in _PLACEHOLDER_KEYS


def _ask_name(language: str) -> str:
    return "मरीज़ का पूरा नाम क्या है?" if language == "hi" else "What is the patient's full name?"


def _new_reference() -> str:
    return str(secrets.randbelow(900_000) + 100_000)


async def book(
    conn: Any, clinic: Clinic, *, doctor_code: str, slot_start: str, patient_name: str, patient_phone: str,
    caller_phone: str | None, reason: str | None, patient_age: Any, language: str, channel: str = "phone",
) -> dict:
    language = speech.lang_or_default(language)
    caller = phones.normalize_phone(caller_phone)
    patient = phones.normalize_phone(patient_phone)
    start = parse_slot(slot_start, clinic)
    matches = await find_doctors(conn, clinic, doctor=doctor_code) if doctor_code and doctor_code != NOTHING else []
    doc = matches[0] if len(matches) == 1 and matches[0]["code"].lower() == doctor_code.lower() else None
    name = (patient_name or "").strip()
    if is_placeholder_name(name):
        return {"ok": False, "spoken": _ask_name(language)}
    if doc is None or start is None or patient is None or not name:
        return {"ok": False, "spoken": ("वह बुकिंग नहीं हो सकी। चलिए कोई दूसरा समय देखते हैं।" if language == "hi"
                                        else "I couldn't make that booking. Let's look at another time.")}
    if not await _slot_is_bookable(conn, clinic, doc, start, holder=caller or patient):
        sorry = "माफ़ कीजिए, वह समय अभी-अभी बुक हो गया। " if language == "hi" else "Sorry, that time was just taken. "
        return {"ok": False, "spoken": sorry + await _alternatives_sentence(conn, clinic, doc, start, language,
                                                                           caller or patient)}
    try:
        age = int(patient_age) if patient_age not in (None, "") else None
    except (TypeError, ValueError):
        age = None
    if age is not None and not 0 <= age <= 130:
        age = None
    end = start + datetime.timedelta(minutes=doc["slot_minutes"])
    reminder_at = reminders_schedule.first_reminder_at(start, clinic.now, clinic.settings, clinic.tz)

    row = None
    for _ in range(5):
        try:
            async with conn.transaction():  # savepoint: a collision must not abort the outer transaction
                row = await conn.fetchrow(
                    "INSERT INTO clinic_appointments (tenant_id, reference, doctor_id, patient_name, patient_phone, "
                    "  booked_by_phone, patient_age, reason, language, channel, starts_at, ends_at, "
                    "  next_reminder_at, reminder_status) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING *",
                    clinic.tenant_id, _new_reference(), doc["id"], name[:120], patient, caller, age,
                    (reason or "").strip()[:500], language, channel, start, end, reminder_at,
                    "pending" if reminder_at else "skipped",
                )
            break
        except asyncpg.ExclusionViolationError:
            sorry = "माफ़ कीजिए, वह समय अभी-अभी बुक हो गया। " if language == "hi" else "Sorry, that time was just taken. "
            return {"ok": False, "spoken": sorry + await _alternatives_sentence(conn, clinic, doc, start, language,
                                                                               caller or patient)}
        except asyncpg.UniqueViolationError:
            continue  # reference collision: draw another
    if row is None:
        raise RuntimeError("could not allocate a booking reference")
    appointment = dict(row)
    await conn.execute(
        "DELETE FROM clinic_slot_holds WHERE doctor_id = $1 AND starts_at = $2", doc["id"], start,
    )
    await notifications.queue_calendar_sync(conn, settings=clinic.settings, appointment=appointment)
    await notifications.queue_sms(
        conn, settings=clinic.settings, appointment=appointment, doctor=doc, department=_department_of(doc),
        local_start=clinic.local(start), event="booked",
    )
    log.info("clinic: booked tenant=%s ref=%s doctor=%s start=%s", clinic.tenant_id, appointment["reference"],
             doc["code"], start.isoformat())

    when = clinic.when(start, language)
    ref = speech.spoken_reference(appointment["reference"])
    location = doc["department_location"]
    sms = clinic.settings.get("sms_enabled")
    if language == "hi":
        spoken = (f"आपका अपॉइंटमेंट बुक हो गया है: {speech.doctor_name(doc, language)} के साथ {when}। "
                  f"आपका बुकिंग नंबर है {ref}।"
                  + (f" कृपया 15 मिनट पहले {location} पहुँचें।" if location else " कृपया 15 मिनट पहले पहुँचें।")
                  + (" आपको SMS भी भेजा जा रहा है।" if sms else "")
                  + " क्या मैं और कुछ मदद कर सकती हूँ?")
    else:
        spoken = (f"Your appointment is booked with {speech.doctor_name(doc, language)} {when}. "
                  f"Your booking number is {ref}."
                  + (f" Please arrive 15 minutes early at {location}." if location else
                     " Please arrive 15 minutes early.")
                  + (" You'll also get an SMS with these details." if sms else "")
                  + " Is there anything else I can help with?")
    return {
        "ok": True, "reference": appointment["reference"], "doctor_code": doc["code"],
        "slot_start": clinic.local(start).isoformat(), "spoken": spoken,
    }


# ── find ─────────────────────────────────────────────────────────────────

def _appointment_view(clinic: Clinic, appointment: dict, doctor: dict, language: str) -> dict:
    return {
        "reference": appointment["reference"],
        "patient_name": appointment["patient_name"],
        "doctor_code": doctor["code"],
        "doctor": speech.doctor_name(doctor, language),
        "department": speech.department_name(_department_of(doctor), language),
        "slot_start": clinic.local(appointment["starts_at"]).isoformat(),
        "spoken": clinic.when(appointment["starts_at"], language),
        "status": appointment["status"],
    }


async def find_appointments(
    conn: Any, clinic: Clinic, *, caller_phone: str | None, reference: str | None, patient_phone: str | None,
    language: str,
) -> dict:
    language = speech.lang_or_default(language)
    caller = phones.normalize_phone(caller_phone)
    patient = phones.normalize_phone(patient_phone)
    if reference:
        appointment = await _appointment_by_reference(conn, clinic, reference)
        if appointment is None or not may_manage(appointment, caller, patient):
            # Same answer whether or not the reference exists: no probing other people's bookings.
            return {"ok": True, "appointments": [], "message": _not_found_sentence(language)}
        rows = [appointment]
    else:
        # Without a booking number only the number the call came from is trusted: a spoken number is ignored.
        if not caller:
            return {"ok": True, "appointments": [], "message": _not_found_sentence(language)}
        rows = [dict(r) for r in await conn.fetch(
            "SELECT * FROM clinic_appointments WHERE tenant_id = $1 AND status = 'booked' AND starts_at > now() "
            "AND (patient_phone = $2 OR booked_by_phone = $2) ORDER BY starts_at LIMIT 5",
            clinic.tenant_id, caller,
        )]
    views = []
    for appointment in rows:
        doc = await _doctor_by_id(conn, clinic, appointment["doctor_id"])
        if doc is not None:
            views.append(_appointment_view(clinic, appointment, doc, language))
    return {
        "ok": True, "appointments": views,
        "message": ("Read the matching appointment back using 'spoken' and confirm which one the caller means."
                    if views else _not_found_sentence(language)),
    }


async def _manageable(
    conn: Any, clinic: Clinic, reference: str | None, caller: str | None, patient: str | None,
) -> dict | None:
    appointment = await _appointment_by_reference(conn, clinic, reference)
    if appointment is None or appointment["status"] != "booked" or appointment["starts_at"] <= clinic.now:
        return None
    return appointment if may_manage(appointment, caller, patient) else None


# ── reschedule ───────────────────────────────────────────────────────────

async def prepare_reschedule(
    conn: Any, clinic: Clinic, *, reference: str, new_slot_start: str, caller_phone: str | None,
    patient_phone: str | None, language: str, doctor: str | None = None,
) -> dict:
    """Same doctor by default; `doctor` moves the booking to another doctor, keeping its number."""
    language = speech.lang_or_default(language)
    nothing = {"reference": "", "new_slot_start": "", "doctor_code": ""}
    caller = phones.normalize_phone(caller_phone)
    appointment = await _manageable(conn, clinic, reference, caller, phones.normalize_phone(patient_phone))
    if appointment is None:
        return _refusal(_not_found_sentence(language), **nothing)
    old_doc = await _doctor_by_id(conn, clinic, appointment["doctor_id"])
    doc = old_doc
    if doctor and doctor.strip():
        matches = await find_doctors(conn, clinic, doctor=doctor)
        if len(matches) != 1:
            return _refusal("आप किस डॉक्टर के पास जाना चाहेंगे?" if language == "hi"
                            else "Which doctor would you like to move it to?", **nothing)
        doc = matches[0]
    start = parse_slot(new_slot_start, clinic)
    holder = caller or appointment["patient_phone"]
    unchanged = doc is not None and old_doc is not None and doc["id"] == old_doc["id"] \
        and start == appointment["starts_at"]
    if doc is None or start is None or unchanged or not await _slot_is_bookable(
        conn, clinic, doc, start, holder=holder, exclude_appointment_id=appointment["id"],
    ):
        sorry = "माफ़ कीजिए, वह समय उपलब्ध नहीं है। " if language == "hi" else "Sorry, that time isn't available. "
        alternatives = await _alternatives_sentence(conn, clinic, doc, start, language, holder,
                                                    exclude_appointment_id=appointment["id"]) if doc else ""
        return _refusal(sorry + alternatives, **nothing)
    if not await _hold_slot(conn, clinic, doc["id"], start, holder):
        return _refusal("माफ़ कीजिए, यह समय अभी कोई और बुक कर रहा है।" if language == "hi"
                        else "Sorry, someone else is booking that time right now.", **nothing)
    old_when = clinic.when(appointment["starts_at"], language)
    new_when = clinic.when(start, language)
    old_name = speech.doctor_name(old_doc, language) if old_doc else ""
    new_name = speech.doctor_name(doc, language)
    patient = appointment["patient_name"]
    if old_doc is not None and doc["id"] == old_doc["id"]:
        text = (f"पुष्टि कर लूँ: {new_name} के साथ {patient} का अपॉइंटमेंट {old_when} से बदलकर {new_when} कर दूँ?"
                if language == "hi" else
                f"Just to confirm: move {patient}'s appointment with {new_name} from {old_when} to {new_when}?")
    else:
        text = (f"पुष्टि कर लूँ: {patient} का {old_name} के साथ {old_when} का अपॉइंटमेंट बदलकर {new_name} के साथ "
                f"{new_when} कर दूँ? बुकिंग नंबर वही रहेगा।" if language == "hi" else
                f"Just to confirm: move {patient}'s appointment from {old_name} {old_when} to {new_name} {new_when}? "
                "The booking number stays the same.")
    return {"ok": True, "reference": appointment["reference"], "new_slot_start": clinic.local(start).isoformat(),
            "doctor_code": doc["code"], "confirm_text": text}


async def reschedule(
    conn: Any, clinic: Clinic, *, reference: str, new_slot_start: str, caller_phone: str | None,
    patient_phone: str | None, language: str, doctor_code: str | None = None,
) -> dict:
    language = speech.lang_or_default(language)
    caller = phones.normalize_phone(caller_phone)
    appointment = await _manageable(conn, clinic, reference, caller, phones.normalize_phone(patient_phone))
    start = parse_slot(new_slot_start, clinic)
    doc = None
    if appointment is not None and doctor_code != NOTHING:   # NOTHING: prepare refused, so nothing moves
        if doctor_code:
            matches = await find_doctors(conn, clinic, doctor=doctor_code)
            doc = matches[0] if len(matches) == 1 and matches[0]["code"].lower() == doctor_code.lower() else None
        else:
            doc = await _doctor_by_id(conn, clinic, appointment["doctor_id"])
    if appointment is None or doc is None or start is None:
        return {"ok": False, "spoken": _not_found_sentence(language)}
    holder = caller or appointment["patient_phone"]
    if not await _slot_is_bookable(conn, clinic, doc, start, holder=holder, exclude_appointment_id=appointment["id"]):
        sorry = "माफ़ कीजिए, वह समय अभी-अभी बुक हो गया। " if language == "hi" else "Sorry, that time was just taken. "
        return {"ok": False, "spoken": sorry + await _alternatives_sentence(
            conn, clinic, doc, start, language, holder, exclude_appointment_id=appointment["id"])}
    reminder_at = reminders_schedule.first_reminder_at(start, clinic.now, clinic.settings, clinic.tz)
    was_reminder_call = appointment["reminder_status"] == "calling"
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE clinic_appointments SET doctor_id = $6, starts_at = $2, ends_at = $3, next_reminder_at = $4, "
                "  reminder_status = $5, reminder_attempts = 0, updated_at = now() "
                "WHERE id = $1 AND status = 'booked' RETURNING *",
                appointment["id"], start, start + datetime.timedelta(minutes=doc["slot_minutes"]), reminder_at,
                "rescheduled" if was_reminder_call else ("pending" if reminder_at else "skipped"), doc["id"],
            )
    except asyncpg.ExclusionViolationError:
        row = None
    if row is None:
        return {"ok": False, "spoken": ("माफ़ कीजिए, वह समय अभी-अभी बुक हो गया।" if language == "hi"
                                        else "Sorry, that time was just taken.")}
    updated = dict(row)
    await conn.execute("DELETE FROM clinic_slot_holds WHERE doctor_id = $1 AND starts_at = $2", doc["id"], start)
    await notifications.queue_calendar_sync(conn, settings=clinic.settings, appointment=updated)
    await notifications.queue_sms(
        conn, settings=clinic.settings, appointment=updated, doctor=doc, department=_department_of(doc),
        local_start=clinic.local(start), event="rescheduled",
    )
    log.info("clinic: rescheduled tenant=%s ref=%s start=%s", clinic.tenant_id, updated["reference"], start.isoformat())
    when = clinic.when(start, language)
    spoken = (f"हो गया। आपका अपॉइंटमेंट अब {speech.doctor_name(doc, language)} के साथ {when} है। "
              f"बुकिंग नंबर वही है: {speech.spoken_reference(updated['reference'])}।"
              if language == "hi" else
              f"Done. Your appointment is now with {speech.doctor_name(doc, language)} {when}. "
              f"Your booking number stays {speech.spoken_reference(updated['reference'])}.")
    return {"ok": True, "reference": updated["reference"], "slot_start": clinic.local(start).isoformat(),
            "spoken": spoken}


# ── cancel ───────────────────────────────────────────────────────────────

async def prepare_cancel(
    conn: Any, clinic: Clinic, *, reference: str, caller_phone: str | None, patient_phone: str | None,
    language: str,
) -> dict:
    language = speech.lang_or_default(language)
    appointment = await _manageable(conn, clinic, reference, phones.normalize_phone(caller_phone),
                                    phones.normalize_phone(patient_phone))
    doc = await _doctor_by_id(conn, clinic, appointment["doctor_id"]) if appointment else None
    if appointment is None or doc is None:
        return _refusal(_not_found_sentence(language), reference="")
    when = clinic.when(appointment["starts_at"], language)
    name = speech.doctor_name(doc, language)
    text = (f"पुष्टि कर लूँ: {appointment['patient_name']} का {name} के साथ {when} का अपॉइंटमेंट रद्द कर दूँ?"
            if language == "hi" else
            f"Just to confirm: cancel {appointment['patient_name']}'s appointment with {name} {when}?")
    return {"ok": True, "reference": appointment["reference"], "confirm_text": text}


async def cancel(
    conn: Any, clinic: Clinic, *, reference: str, caller_phone: str | None, patient_phone: str | None,
    reason: str | None, language: str,
) -> dict:
    language = speech.lang_or_default(language)
    appointment = await _manageable(conn, clinic, reference, phones.normalize_phone(caller_phone),
                                    phones.normalize_phone(patient_phone))
    doc = await _doctor_by_id(conn, clinic, appointment["doctor_id"]) if appointment else None
    if appointment is None or doc is None:
        return {"ok": False, "spoken": _not_found_sentence(language)}
    row = await conn.fetchrow(
        "UPDATE clinic_appointments SET status = 'cancelled', cancelled_at = now(), cancel_reason = $2, "
        "  next_reminder_at = NULL, "
        "  reminder_status = CASE WHEN reminder_status = 'calling' THEN 'cancelled' ELSE reminder_status END, "
        "  updated_at = now() "
        "WHERE id = $1 AND status = 'booked' RETURNING *",
        appointment["id"], (reason or "").strip()[:500] or None,
    )
    if row is None:
        return {"ok": False, "spoken": _not_found_sentence(language)}
    cancelled = dict(row)
    await notifications.queue_calendar_sync(conn, settings=clinic.settings, appointment=cancelled)
    await notifications.queue_sms(
        conn, settings=clinic.settings, appointment=cancelled, doctor=doc, department=_department_of(doc),
        local_start=clinic.local(cancelled["starts_at"]), event="cancelled",
    )
    log.info("clinic: cancelled tenant=%s ref=%s", clinic.tenant_id, cancelled["reference"])
    spoken = ("आपका अपॉइंटमेंट रद्द कर दिया गया है। क्या आप कोई नया समय बुक करना चाहेंगे?"
              if language == "hi" else
              "Your appointment has been cancelled. Would you like to book a new time?")
    return {"ok": True, "reference": cancelled["reference"], "spoken": spoken}


# ── reminder calls ───────────────────────────────────────────────────────

async def reminder_context(conn: Any, clinic: Clinic, *, caller_phone: str | None, language: str) -> dict:
    """The appointment this reminder call is about: the one being called for on this number."""
    language = speech.lang_or_default(language)
    caller = phones.normalize_phone(caller_phone)
    row = await conn.fetchrow(
        "SELECT * FROM clinic_appointments WHERE tenant_id = $1 AND patient_phone = $2 AND status = 'booked' "
        "AND reminder_status = 'calling' AND starts_at > now() ORDER BY starts_at LIMIT 1",
        clinic.tenant_id, caller,
    ) if caller else None
    if row is None:
        return {"ok": False, "message": "No reminder is in progress for this number. Offer general help."}
    appointment = dict(row)
    doc = await _doctor_by_id(conn, clinic, appointment["doctor_id"])
    if doc is None:
        return {"ok": False, "message": "No reminder is in progress for this number. Offer general help."}
    view = _appointment_view(clinic, appointment, doc, language)
    when = view["spoken"]
    view["spoken_reminder"] = (
        f"{appointment['patient_name']} जी, {doc['department_location'] or clinic.settings['hospital_name']} में "
        f"{view['doctor']} के साथ आपका अपॉइंटमेंट {when} है। क्या आप आ पाएँगे?"
        if language == "hi" else
        f"{appointment['patient_name']} has an appointment with {view['doctor']} {when}. Will you be able to make it?"
    )
    return {"ok": True, "appointment": view}


async def record_reminder_outcome(
    conn: Any, clinic: Clinic, *, reference: str, outcome: str, caller_phone: str | None, language: str,
) -> dict:
    language = speech.lang_or_default(language)
    if outcome not in ("confirmed", "declined"):
        return {"ok": False, "spoken": "outcome must be confirmed or declined"}
    appointment = await _manageable(conn, clinic, reference, phones.normalize_phone(caller_phone), None)
    if appointment is None:
        return {"ok": False, "spoken": _not_found_sentence(language)}
    await conn.execute(
        "UPDATE clinic_appointments SET reminder_status = $2, next_reminder_at = NULL, updated_at = now() "
        "WHERE id = $1", appointment["id"], outcome,
    )
    if outcome == "confirmed":
        spoken = ("धन्यवाद! हम आपका इंतज़ार करेंगे। कृपया 15 मिनट पहले पहुँचें।" if language == "hi"
                  else "Thank you! We'll see you then. Please arrive 15 minutes early.")
    else:
        spoken = ("ठीक है। क्या आप इसे किसी और समय पर करना चाहेंगे, या रद्द कर दूँ?" if language == "hi"
                  else "No problem. Would you like to move it to another time, or shall I cancel it?")
    return {"ok": True, "reference": appointment["reference"], "spoken": spoken}
