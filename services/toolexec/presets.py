"""
services/toolexec/presets.py — connector presets: static definitions that
write ordinary custom_apis rows, plus the pure functions the executor runs
against them.

A preset is a template, not a runtime abstraction: applying one inserts rows
through custom_apis._insert_custom_api, and from then on the executor treats
them like any other API. Everything here that touches a call is pure so it can
be tested without a database: the remote-party rule for the `caller_id` param
source, the response transforms (a closed set, written only by this module),
the read-back rendering and the booking-claim release target.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

import asyncpg
from pydantic import BaseModel

from libs.config_sdk.secrets import encrypt_tenant_secret
from libs.tenancy import tenant_conn

from . import audit, custom_apis, db, graph, oauth, redaction
from .custom_apis import DependentApiExists, resolve_and_validate_endpoint
from .schemas import (
    CalendarBookingSetup, HubspotCrmSetup, SalesforceCrmSetup, SheetsLeadCaptureSetup, WhatsAppSetup, ZohoCrmSetup,
)

# Most successful sends one WhatsApp row may make per call session. A flat
# constant written into the row at apply time: a tenant-settable cap would be
# a tenant-settable spam budget.
SEND_CAP = 3

_GOOGLE_CALENDAR = "https://www.googleapis.com/calendar/v3"
_CALENDAR_SCOPES = frozenset({
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
})
_SHEETS_SCOPES = frozenset({"https://www.googleapis.com/auth/drive.file"})
_SHEETS_HEADER = ["Name", "Phone", "Email", "Notes"]


@dataclass(frozen=True)
class PresetParam:
    name: str
    location: str
    source: str
    json_type: str = "string"
    description: str = ""
    required: bool = True
    literal_value: Any = None
    upstream_step: str | None = None
    upstream_json_path: str | None = None
    sensitive: bool = False
    body_path: str | None = None
    value_prefix: str | None = None
    value_digits_only: bool = False

    def as_row(self, api_ids: dict[str, Any]) -> dict:
        """The param dict custom_apis._replace_params writes; `api_ids` maps the
        preset's step names to the ids of the rows already inserted."""
        return {
            "name": self.name, "location": self.location, "json_type": self.json_type,
            "description": self.description, "required": self.required, "source": self.source,
            "literal_value": self.literal_value,
            "upstream_api_id": api_ids[self.upstream_step] if self.upstream_step else None,
            "upstream_json_path": self.upstream_json_path, "sensitive": self.sensitive,
            "body_path": self.body_path, "value_prefix": self.value_prefix,
            "value_digits_only": self.value_digits_only,
        }


@dataclass(frozen=True)
class PresetStep:
    name: str
    description: str
    method: str
    endpoint_url: str
    params: tuple[PresetParam, ...]
    side_effecting: bool
    body_style: str = "json"
    auth_scheme: str = "oauth2_authorization_code"
    # The credential ref itself is added at apply time (WhatsApp) or lives on
    # the connection row (OAuth), so this holds only the non-secret shape.
    auth_config: dict = field(default_factory=dict)
    success_template: str | None = None
    response_transform: dict | None = None
    idempotency_body_field: str | None = None
    confirmation_template: str | None = None
    session_send_cap: int | None = None
    # "oauth_connection": endpoint_url is a path and the origin is the connection's.
    endpoint_base_source: str = "literal"


@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    setup_model: type[BaseModel]
    provider: str | None                     # the OAuth provider it needs, if any
    scopes: frozenset[str]                   # beyond the provider's identity scopes
    steps: Callable[..., list[PresetStep]]
    # step name -> the step whose claim a successful call of it releases
    releases_claim_of: dict[str, str] = field(default_factory=dict)


def _caller(name: str, description: str, *, location: str = "body", body_path: str | None = None,
            required: bool = True) -> PresetParam:
    return PresetParam(name, location, "caller", description=description, required=required, body_path=body_path)


def _literal(name: str, value: Any, *, location: str = "body", body_path: str | None = None,
             json_type: str = "string") -> PresetParam:
    return PresetParam(name, location, "literal", json_type=json_type, literal_value=value, body_path=body_path)


def _caller_id(name: str, *, location: str = "body", body_path: str | None = None,
               value_prefix: str | None = None, digits_only: bool = False) -> PresetParam:
    # Always sensitive: it still feeds the arguments hash, but the number never
    # reaches arguments_redacted, which the whole tenant can read.
    return PresetParam(
        name, location, "caller_id", sensitive=True, body_path=body_path,
        value_prefix=value_prefix, value_digits_only=digits_only,
    )


def _upstream(name: str, step: str, json_path: str) -> PresetParam:
    return PresetParam(name, "path", "upstream", upstream_step=step, upstream_json_path=json_path)


# ── calendar booking ──────────────────────────────────────────────────────

def _calendar_steps(setup: CalendarBookingSetup) -> list[PresetStep]:
    events = f"{_GOOGLE_CALENDAR}/calendars/{quote(setup.calendar_id, safe='')}/events"
    find_event_id = _upstream("event_id", "gcal_find_booking", "$.items[0].id")
    return [
        PresetStep(
            name="gcal_check_slots",
            description="List free appointment times between two moments.",
            method="POST", endpoint_url=f"{_GOOGLE_CALENDAR}/freeBusy", side_effecting=False,
            params=(
                _caller("time_min", "Start of the window to search, ISO 8601 with UTC offset.", body_path="timeMin"),
                _caller("time_max", "End of the window to search, ISO 8601 with UTC offset.", body_path="timeMax"),
                _literal("calendar_id", setup.calendar_id, body_path="items.0.id"),
            ),
            response_transform={
                "kind": "google_freebusy_slots", "timezone": setup.timezone, "day_start": setup.day_start,
                "day_end": setup.day_end, "slot_minutes": setup.slot_minutes,
            },
            success_template="Available times: {{$.spoken_slots}}.",
        ),
        PresetStep(
            name="gcal_book",
            description="Book an appointment for the person on this call.",
            method="POST", endpoint_url=events, side_effecting=True,
            params=(
                _caller("patient_name", "Name to book the appointment under.", body_path="summary"),
                _caller("service", "The doctor or service being booked.",
                        body_path="extendedProperties.private.service"),
                _caller("start_time", "Start, ISO 8601 with UTC offset.", body_path="start.dateTime"),
                _caller("end_time", "End, ISO 8601 with UTC offset.", body_path="end.dateTime"),
                _caller_id("caller_phone", body_path="extendedProperties.private.yuviz_phone"),
                _literal("yuviz_preset", "calendar_booking", body_path="extendedProperties.private.yuviz_preset"),
            ),
            idempotency_body_field="id",
            confirmation_template=(
                "To confirm: {{$.summary}}, {{$.extendedProperties.private.service}}, "
                "on {{$.start.dateTime}}. Shall I book it?"
            ),
            success_template="Booked {{$.summary}} for {{$.start.dateTime}}.",
        ),
        PresetStep(
            name="gcal_find_booking",
            description="Find the upcoming appointment booked from this phone number.",
            method="GET", endpoint_url=events, side_effecting=False,
            params=(
                _caller("timeMin", "Earliest start to look from, ISO 8601 with UTC offset.", location="query"),
                _caller_id("privateExtendedProperty", location="query", value_prefix="yuviz_phone="),
                _literal("singleEvents", "true", location="query"),
                _literal("orderBy", "startTime", location="query"),
                _literal("maxResults", "5", location="query"),
            ),
            response_transform={"kind": "google_booking_lookup"},
        ),
        PresetStep(
            name="gcal_reschedule",
            description="Move the caller's booked appointment to a new time.",
            method="PATCH", endpoint_url=f"{events}/{{event_id}}", side_effecting=True,
            params=(
                find_event_id,
                _caller("start_time", "New start, ISO 8601 with UTC offset.", body_path="start.dateTime"),
                _caller("end_time", "New end, ISO 8601 with UTC offset.", body_path="end.dateTime"),
            ),
            response_transform={"kind": "google_event_projection"},
            confirmation_template=(
                "To confirm, move your {{$.upstream.gcal_find_booking.items[0].service}} appointment on "
                "{{$.upstream.gcal_find_booking.items[0].start}} to {{$.start.dateTime}}?"
            ),
            success_template="Moved your appointment to {{$.start}}.",
        ),
        PresetStep(
            name="gcal_cancel",
            description="Cancel the caller's booked appointment.",
            method="DELETE", endpoint_url=f"{events}/{{event_id}}", side_effecting=True,
            params=(find_event_id,),
            confirmation_template=(
                "To confirm, cancel your {{$.upstream.gcal_find_booking.items[0].service}} appointment on "
                "{{$.upstream.gcal_find_booking.items[0].start}}?"
            ),
            success_template="Your appointment has been cancelled.",
        ),
    ]


# ── CRM contact lookup ────────────────────────────────────────────────────

_CRM_SCOPES = {
    "salesforce": frozenset({"api", "refresh_token"}),
    "hubspot": frozenset({"oauth", "crm.objects.contacts.read"}),
    "zoho": frozenset({"ZohoCRM.modules.contacts.READ"}),
}

# Scopes the account-level Connect (no preset) asks for on top of the identity
# scopes, so a bare Connect yields a connection the CRM preset can use as-is.
# HubSpot has no identity scope and refuses an install URL that omits a required
# one. Salesforce returns no refresh_token unless the refresh_token scope is
# asked for. Zoho gets its refresh token from access_type=offline (already in
# its extra_authorize_params); the scope here is the CRM data scope it needs.
CONNECT_SCOPES: dict[str, frozenset[str]] = {
    "hubspot": _CRM_SCOPES["hubspot"],
    "salesforce": _CRM_SCOPES["salesforce"],
    "zoho": _CRM_SCOPES["zoho"],
}


def _crm_lookup_step(
    provider: str, *, method: str, endpoint_url: str, params: tuple[PresetParam, ...],
    endpoint_base_source: str,
) -> list[PresetStep]:
    return [PresetStep(
        name="crm_lookup_contact",
        description="Look up the person on this call in the CRM by their phone number.",
        method=method, endpoint_url=endpoint_url, side_effecting=False, params=params,
        endpoint_base_source=endpoint_base_source,
        response_transform={"kind": "crm_contact_projection", "provider": provider},
        success_template="Contact lookup: {{$.outcome}}.{{$.spoken}}",
    )]


def _salesforce_steps(setup: SalesforceCrmSetup) -> list[PresetStep]:
    return _crm_lookup_step(
        "salesforce", method="GET", endpoint_url="/services/data/v61.0/parameterizedSearch/",
        endpoint_base_source="oauth_connection",
        params=(
            # '+' is reserved in SOSL, so the number goes in as digits.
            _caller_id("q", location="query", digits_only=True),
            _literal("sobject", "Contact", location="query"),
            _literal("Contact.fields", "Id,Name,Phone", location="query"),
            _literal("Contact.limit", "5", location="query"),
        ),
    )


def _hubspot_steps(setup: HubspotCrmSetup) -> list[PresetStep]:
    # filterGroups are OR-ed: the number is sent both as E.164 and as bare digits.
    group = lambda i: (  # noqa: E731
        _literal(f"group_{i}_property", "phone", body_path=f"filterGroups.{i}.filters.0.propertyName"),
        _literal(f"group_{i}_operator", "EQ", body_path=f"filterGroups.{i}.filters.0.operator"),
    )
    return _crm_lookup_step(
        "hubspot", method="POST", endpoint_url="https://api.hubapi.com/crm/v3/objects/contacts/search",
        endpoint_base_source="literal",
        params=(
            _caller_id("phone_e164", body_path="filterGroups.0.filters.0.value"),
            _caller_id("phone_digits", body_path="filterGroups.1.filters.0.value", digits_only=True),
            *group(0), *group(1),
            _literal("properties", ["firstname", "lastname", "company", "phone"], json_type="array"),
            _literal("limit", 5, json_type="integer"),
        ),
    )


def _zoho_steps(setup: ZohoCrmSetup) -> list[PresetStep]:
    return _crm_lookup_step(
        "zoho", method="GET", endpoint_url="/crm/v3/Contacts/search", endpoint_base_source="oauth_connection",
        params=(
            _caller_id("phone", location="query"),
            _literal("fields", "id,Full_Name,Phone,Account_Name,Owner", location="query"),
        ),
    )


# ── WhatsApp confirmation ─────────────────────────────────────────────────

def _template_variables(setup: WhatsAppSetup, body_path: Callable[[int], str]) -> list[PresetParam]:
    return [
        _caller(f"body_{i + 1}", f"Value for template variable {i + 1}.", body_path=body_path(i))
        for i in range(setup.param_count)
    ]


def _whatsapp_steps(setup: WhatsAppSetup) -> list[PresetStep]:
    common = dict(
        name="whatsapp_send_confirmation",
        description=(
            "Send a WhatsApp confirmation message to the person on this call. "
            "The message always goes to the number on the call."
        ),
        method="POST", side_effecting=True, session_send_cap=SEND_CAP,
    )
    if setup.provider == "gupshup":
        return [PresetStep(
            **common, endpoint_url="https://api.gupshup.io/wa/api/v1/template/msg", body_style="form",
            auth_scheme="api_key", auth_config={"name": "apikey", "location": "header"},
            params=(
                _literal("channel", "whatsapp"),
                _literal("source", setup.source_number),
                _literal("src.name", setup.app_name),
                _caller_id("destination", digits_only=True),
                _literal("template_id", setup.template, body_path="template.id"),
                *_template_variables(setup, lambda i: f"template.params.{i}"),
            ),
        )]
    if setup.provider == "interakt":
        return [PresetStep(
            **common, endpoint_url="https://api.interakt.ai/v1/public/message/",
            auth_scheme="api_key", auth_config={"name": "Authorization", "location": "header"},
            params=(
                _caller_id("fullPhoneNumber"),
                _literal("type", "Template"),
                _literal("template_name", setup.template, body_path="template.name"),
                _literal("template_language", setup.language, body_path="template.languageCode"),
                *_template_variables(setup, lambda i: f"template.bodyValues.{i}"),
            ),
        )]
    # Meta Cloud API
    components: list[PresetParam] = []
    if setup.param_count:
        components.append(_literal("component_type", "body", body_path="template.components.0.type"))
        for i in range(setup.param_count):
            components.append(_literal(
                f"body_{i + 1}_type", "text", body_path=f"template.components.0.parameters.{i}.type",
            ))
        components.extend(_template_variables(setup, lambda i: f"template.components.0.parameters.{i}.text"))
    return [PresetStep(
        **common, endpoint_url=f"https://graph.facebook.com/v20.0/{setup.phone_number_id}/messages",
        auth_scheme="bearer",
        params=(
            _literal("messaging_product", "whatsapp"),
            _caller_id("to", digits_only=True),
            _literal("type", "template"),
            _literal("template_name", setup.template, body_path="template.name"),
            _literal("template_language", setup.language, body_path="template.language.code"),
            *components,
        ),
    )]


# ── Google Sheets lead capture ────────────────────────────────────────────

def _sheets_steps(setup: SheetsLeadCaptureSetup, *, spreadsheet_id: str) -> list[PresetStep]:
    return [PresetStep(
        name="sheets_capture_lead",
        description="Add a lead (a caller who wants to be contacted) as a new row in the leads sheet.",
        method="POST",
        endpoint_url="https://sheets.googleapis.com/v4/spreadsheets/{spreadsheetId}/values/Sheet1!A1:append",
        side_effecting=True,
        params=(
            _literal("spreadsheetId", spreadsheet_id, location="path"),
            # RAW: Sheets stores a caller-supplied "=IMPORTXML(...)" as text and never evaluates it.
            _literal("valueInputOption", "RAW", location="query"),
            _caller("lead_name", "The lead's name.", body_path="values.0.0"),
            PresetParam("lead_phone", "body", "caller", description="The lead's phone number.",
                        sensitive=True, body_path="values.0.1"),
            PresetParam("lead_email", "body", "caller", description="The lead's email, if given.",
                        required=False, sensitive=True, body_path="values.0.2"),
            _caller("notes", "What the lead wants, in a few words.", body_path="values.0.3", required=False),
        ),
    )]


PRESETS: dict[str, Preset] = {
    "calendar_booking": Preset(
        key="calendar_booking", title="Google Calendar booking", setup_model=CalendarBookingSetup,
        provider="google", scopes=_CALENDAR_SCOPES, steps=_calendar_steps,
        releases_claim_of={"gcal_cancel": "gcal_book"},
    ),
    "whatsapp_confirmation": Preset(
        key="whatsapp_confirmation", title="WhatsApp confirmation", setup_model=WhatsAppSetup,
        provider=None, scopes=frozenset(), steps=_whatsapp_steps,
    ),
    "sheets_lead_capture": Preset(
        key="sheets_lead_capture", title="Google Sheets lead capture", setup_model=SheetsLeadCaptureSetup,
        provider="google", scopes=_SHEETS_SCOPES, steps=_sheets_steps,
    ),
    "salesforce_crm": Preset(
        key="salesforce_crm", title="Salesforce contact lookup", setup_model=SalesforceCrmSetup,
        provider="salesforce", scopes=_CRM_SCOPES["salesforce"], steps=_salesforce_steps,
    ),
    "hubspot_crm": Preset(
        key="hubspot_crm", title="HubSpot contact lookup", setup_model=HubspotCrmSetup,
        provider="hubspot", scopes=_CRM_SCOPES["hubspot"], steps=_hubspot_steps,
    ),
    "zoho_crm": Preset(
        key="zoho_crm", title="Zoho CRM contact lookup", setup_model=ZohoCrmSetup,
        provider="zoho", scopes=_CRM_SCOPES["zoho"], steps=_zoho_steps,
    ),
}


# ── the caller-id param source ────────────────────────────────────────────

def normalize_ani(raw: str) -> str | None:
    """'+' and the digits, if there are 8 to 15 of them once everything else is
    dropped. Not E.164 validation: book and find both go through this, so the
    stored and the looked-up value always match byte for byte."""
    digits = re.sub(r"\D", "", raw or "")
    return "+" + digits if 8 <= len(digits) <= 15 else None


def remote_party_number(call_direction: str, caller_number: str, called_number: str) -> str | None:
    """The party at the other end of the call, from the call's own server-set
    metadata: the caller on an inbound call, the callee on an outbound one
    (where `caller_number` is the tenant's own DID). Every other direction, an
    unusable number, or two legs that normalize to the same number is None."""
    if call_direction == "inbound":
        remote, other = caller_number, called_number
    elif call_direction == "outbound":
        remote, other = called_number, caller_number
    else:
        return None
    candidate = normalize_ani(remote)
    if candidate is None or candidate == normalize_ani(other):
        return None
    return candidate


# ── response transforms ───────────────────────────────────────────────────

_TRANSFORM_KEYS = {
    "google_freebusy_slots": {"kind", "timezone", "day_start", "day_end", "slot_minutes"},
    "google_booking_lookup": {"kind"},
    "google_event_projection": {"kind"},
    "crm_contact_projection": {"kind", "provider"},
}

# The provider's envelope key: where the candidate records sit in its response.
_CRM_ENVELOPE = {"salesforce": "searchRecords", "hubspot": "results", "zoho": "data"}


def validate_response_transform(transform: Any) -> None:
    kind = transform.get("kind") if isinstance(transform, dict) else None
    if kind not in _TRANSFORM_KEYS:
        raise ValueError("invalid_response_transform: unknown kind")
    if set(transform) != _TRANSFORM_KEYS[kind]:
        raise ValueError("invalid_response_transform: unexpected keys")
    if kind == "crm_contact_projection":
        provider = transform["provider"]
        if not isinstance(provider, str) or provider not in _CRM_ENVELOPE:
            raise ValueError("invalid_response_transform: unknown provider")


def apply_response_transform(
    transform: dict, response: Any, body_fields: dict, *, caller_ani: str | None = None,
) -> dict:
    kind = transform["kind"]
    if kind == "crm_contact_projection":
        # Before the dict check: this kind is total over every response, never raises.
        return _crm_contact_projection(transform["provider"], response, caller_ani)
    if not isinstance(response, dict):
        raise ValueError("response_transform: not a JSON object")
    if kind == "google_freebusy_slots":
        return _freebusy_slots(transform, response, body_fields)
    if kind == "google_booking_lookup":
        return _booking_lookup(response, caller_ani)
    if kind == "google_event_projection":
        return _event_projection(response)
    raise ValueError("invalid_response_transform: unknown kind")


# ── CRM contact projection ────────────────────────────────────────────────

def digits_only(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def phone_suffix_match(record_phone: str, caller_ani: str) -> bool:
    """National-significant-number comparison: the last 9 digits, or the whole
    shorter number when either has fewer than 9, with a 7-digit floor."""
    record, caller = digits_only(record_phone), digits_only(caller_ani)
    if len(record) < 7 or len(caller) < 7:
        return False
    if min(len(record), len(caller)) >= 9:
        return record[-9:] == caller[-9:]
    shorter, longer = sorted((record, caller), key=len)
    return longer.endswith(shorter)


_CRM_FIELD_CAP = 100
_CRM_SPOKEN_CAP = 120  # the executor truncates each template placeholder at 120
_CRM_KEPT_PUNCTUATION = frozenset(".,'-&/()#")


def _crm_clean(value: str | None) -> str | None:
    """CRM text is written by outsiders (web-to-lead, public forms), so it may
    only travel as plain words: letters, marks, digits, spaces and a little
    punctuation. Filter first, cap second, so length cannot carry a payload past
    the filter. Dropping `{ } " \\` and controls is what lets a value neither
    end its JSON string in `items` nor open or close a `{{placeholder}}`."""
    if value is None:
        return None
    kept = []
    for ch in value:
        category = unicodedata.category(ch)
        if category == "Zs":
            kept.append(" ")
        elif category[0] in "LM" or category == "Nd" or ch in _CRM_KEPT_PUNCTUATION:
            kept.append(ch)
    return " ".join("".join(kept).split())[:_CRM_FIELD_CAP].strip() or None


def _crm_str(record: Any, *path: str) -> str | None:
    """The one literal path, and only if it ends on a str. Never a sibling key."""
    for key in path:
        if not isinstance(record, dict):
            return None
        record = record.get(key)
    return record if isinstance(record, str) else None


def _crm_extract(provider: str, record: dict) -> tuple[Any, str | None, str | None, str | None, str | None]:
    """(contact_id, full_name, company, owner_name, record_phone), unfiltered."""
    if provider == "salesforce":
        return (record.get("Id"), _crm_str(record, "Name"), None, None, _crm_str(record, "Phone"))
    if provider == "hubspot":
        names = (_crm_str(record, "properties", "firstname"), _crm_str(record, "properties", "lastname"))
        return (
            record.get("id"), " ".join(filter(None, names)) or None,
            _crm_str(record, "properties", "company"), None, _crm_str(record, "properties", "phone"),
        )
    return (
        record.get("id"), _crm_str(record, "Full_Name"), _crm_str(record, "Account_Name", "name"),
        _crm_str(record, "Owner", "name"), _crm_str(record, "Phone"),
    )


def _crm_result(outcome: str, items: list[dict], spoken: str) -> dict:
    return {"outcome": outcome, "items": items, "spoken": spoken}


def _crm_spoken(item: dict) -> str:
    """The fixed carrier sentence, so CRM text never stands alone as a line.
    Fields are appended in order, stopping before the first that would push it
    past what the executor keeps, never cutting one mid-word."""
    text = "Caller matched:"
    for key, lead in (("full_name", " "), ("company", " at "), ("owner_name", ", account owner ")):
        if item[key] is None:
            continue
        candidate = f"{text}{lead}{item[key]}"
        if len(candidate) + 1 > _CRM_SPOKEN_CAP:
            break
        text = candidate
    return "" if text == "Caller matched:" else text + "."


def _crm_contact_projection(provider: str, response: Any, caller_ani: str | None) -> dict:
    records = response.get(_CRM_ENVELOPE[provider]) if isinstance(response, dict) and "_raw" not in response else None
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records) or caller_ani is None:
        return _crm_result("no_match", [], "")
    survivors = []
    for record in records:
        contact_id, full_name, company, owner_name, record_phone = _crm_extract(provider, record)
        contact_id = _crm_clean(contact_id) if isinstance(contact_id, str) else None
        if (contact_id is not None and record_phone is not None
                and phone_suffix_match(record_phone, caller_ani)):
            survivors.append({
                "contact_id": contact_id, "full_name": _crm_clean(full_name),
                "company": _crm_clean(company), "owner_name": _crm_clean(owner_name),
            })
    if not survivors:
        return _crm_result("no_match", [], "")
    if len(survivors) > 1:
        return _crm_result("ambiguous", [], "")
    return _crm_result("match", survivors, _crm_spoken(survivors[0]))


_MAX_SLOTS = 3


def _parse_dt(value: str, tz: zoneinfo.ZoneInfo) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz)


def _spoken_time(moment: datetime.datetime) -> str:
    hour = moment.hour % 12 or 12
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{hour}{minutes} {'AM' if moment.hour < 12 else 'PM'}"


def _spoken_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])}{',' if len(items) > 2 else ''} or {items[-1]}"


def _freebusy_slots(config: dict, response: dict, body_fields: dict) -> dict:
    tz = zoneinfo.ZoneInfo(config["timezone"])
    window_start = _parse_dt(body_fields["timeMin"], tz)
    window_end = _parse_dt(body_fields["timeMax"], tz)
    opens = datetime.time.fromisoformat(config["day_start"])
    closes = datetime.time.fromisoformat(config["day_end"])
    length = datetime.timedelta(minutes=config["slot_minutes"])

    if "calendars" not in response:
        raise ValueError("freebusy_response_malformed")  # no answer is not "all free"
    busy: list[tuple[datetime.datetime, datetime.datetime]] = []
    for calendar in response["calendars"].values():
        if calendar.get("errors"):
            # Busy time we could not read is not free time.
            raise ValueError("freebusy_calendar_error")
        busy.extend((_parse_dt(b["start"], tz), _parse_dt(b["end"], tz)) for b in calendar.get("busy", []))

    slots: list[tuple[datetime.datetime, datetime.datetime]] = []
    day = window_start.astimezone(tz).date()
    last_day = window_end.astimezone(tz).date()
    while day <= last_day and len(slots) < _MAX_SLOTS:
        slot = datetime.datetime.combine(day, opens, tzinfo=tz)
        close = datetime.datetime.combine(day, closes, tzinfo=tz)
        while slot + length <= close and len(slots) < _MAX_SLOTS:
            end = slot + length
            if (slot >= window_start and end <= window_end
                    and not any(slot < busy_end and busy_start < end for busy_start, busy_end in busy)):
                slots.append((slot, end))
            slot = end
        day += datetime.timedelta(days=1)

    several_days = len({start.date() for start, _end in slots}) > 1
    spoken = [
        f"{start:%A} {_spoken_time(start)}" if several_days else _spoken_time(start) for start, _end in slots
    ]
    return {
        "slots": [
            {"start": start.isoformat(), "end": end.isoformat(), "spoken": text}
            for (start, end), text in zip(slots, spoken)
        ],
        "slot_count": len(slots),
        "spoken_slots": _spoken_list(spoken) if spoken else "none in that window",
    }


_CLAIM_ID_RE = re.compile(r"[0-9a-f]{32}")  # a uuid hex, as gcal_book writes the claim id


def _flatten_event(event: dict) -> dict | None:
    start = (event.get("start") or {}).get("dateTime")
    end = (event.get("end") or {}).get("dateTime")
    if start is None or end is None:
        return None
    private = (event.get("extendedProperties") or {}).get("private") or {}
    return {"id": event.get("id"), "start": start, "end": end, "service": private.get("service", "")}


def _booking_lookup(response: dict, caller_ani: str | None) -> dict:
    """Keeps an event only if every check holds, so a second layer sits under
    Google's own privateExtendedProperty filter: not cancelled, made by this
    preset, stored phone equal to the caller's, and an id that is a claim id
    written by gcal_book. Everything else (the patient's name, description,
    attendees) is dropped here, before it reaches a step row."""
    if caller_ani is None:
        return {"items": [], "match_count": 0}
    matches = []
    for event in response.get("items") or []:
        private = (event.get("extendedProperties") or {}).get("private") or {}
        event_id = event.get("id")
        if (event.get("status") != "cancelled"
                and private.get("yuviz_preset") == "calendar_booking"
                and private.get("yuviz_phone") == caller_ani
                and isinstance(event_id, str) and _CLAIM_ID_RE.fullmatch(event_id)):
            flat = _flatten_event(event)
            if flat is not None:
                matches.append(flat)
    matches.sort(key=lambda item: item["start"])
    return {"items": matches[:1], "match_count": len(matches)}


def _event_projection(response: dict) -> dict:
    return _flatten_event(response) or {"id": response.get("id")}


# ── confirmation read-back ────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]")
_ISO_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _speak(value: Any) -> str:
    text = _CONTROL_CHAR_RE.sub("", str(value))
    if isinstance(value, str) and _ISO_DATETIME_RE.match(value):
        try:
            moment = datetime.datetime.fromisoformat(value)
        except ValueError:
            return text[:120]
        # In the instant's own UTC offset, so the read-back names what will be booked.
        return f"{moment:%A} {moment.day} {moment:%B} at {_spoken_time(moment)}"
    return text[:120]


def render_confirmation(template: str, arguments_redacted: dict, upstream_responses: dict[str, dict]) -> str:
    """Fills `{{$.path}}` placeholders from the redacted arguments and the
    post-transform upstream responses (under `upstream.<api name>`). Raises
    ValueError when a placeholder is missing or only holds the redaction
    sentinel: a read-back that cannot say what will happen must not be spoken."""
    source = {**arguments_redacted, "upstream": upstream_responses}

    def _fill(match: "re.Match[str]") -> str:
        value = graph.extract(source, match.group(1).strip())
        if value is graph.MISSING or value == redaction.REDACTED:
            raise ValueError("confirmation_unrenderable")
        return _speak(value)

    return _PLACEHOLDER_RE.sub(_fill, template)


def claim_release_target(api_row: dict) -> str | None:
    """The step whose side-effect claim a successful call of this row releases
    (gcal_cancel releases gcal_book's), for a preset row that declares one."""
    preset = PRESETS.get(api_row.get("preset_key"))
    return preset.releases_claim_of.get(api_row["name"]) if preset is not None else None


# ── apply ─────────────────────────────────────────────────────────────────

_SECRET_REF_FIELD = {"api_key": "key_ref", "bearer": "token_ref"}


async def _insert_missing_steps(
    conn, tenant_id: str, preset_key: str, steps: list[PresetStep], *,
    oauth_connection_id: Any | None, secret_ref: str | None,
) -> list[dict]:
    """Inserts the steps this tenant does not already have under `preset_key`,
    in the order given (an upstream step must come before its dependents), and
    returns the new rows. The caller holds the tenant's custom_apis advisory
    lock and recomputes chain_levels once afterwards."""
    api_ids: dict[str, Any] = {
        row["name"]: row["id"] for row in await conn.fetch(
            "SELECT id, name FROM custom_apis WHERE tenant_id = $1 AND preset_key = $2 AND deleted_at IS NULL",
            tenant_id, preset_key,
        )
    }
    created = []
    for step in steps:
        if step.name in api_ids:
            continue
        auth_config = dict(step.auth_config)
        if step.auth_scheme in _SECRET_REF_FIELD:
            auth_config[_SECRET_REF_FIELD[step.auth_scheme]] = secret_ref
        try:
            row = await custom_apis._insert_custom_api(
                conn, tenant_id=tenant_id, name=step.name, description=step.description,
                endpoint_url=step.endpoint_url, method=step.method, body_style=step.body_style,
                auth_scheme=step.auth_scheme, auth_config=auth_config, side_effecting=step.side_effecting,
                idempotency_header=None, timeout_ms=None, sensitive_response_paths=[],
                success_template=step.success_template, params=[p.as_row(api_ids) for p in step.params],
                oauth_connection_id=oauth_connection_id if step.auth_scheme == "oauth2_authorization_code" else None,
                preset_key=preset_key, response_transform=step.response_transform,
                idempotency_body_field=step.idempotency_body_field,
                confirmation_template=step.confirmation_template, session_send_cap=step.session_send_cap,
                endpoint_base_source=step.endpoint_base_source,
            )
        except asyncpg.UniqueViolationError:
            # A hand-registered API already has this name; the tenant renames or removes it.
            raise ValueError(f"preset_name_conflict: {step.name}") from None
        api_ids[step.name] = row["id"]
        created.append(row)
    return created


class PresetConnectorRequired(Exception):
    """409: the preset needs a connected account (`connector_required`) or one
    that has been granted more scopes (`connector_scope_required`). The console
    then starts authorization with this preset_key and applies again."""


_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"


async def _create_lead_sheet(tenant_id: str, connection_id: Any, title: str) -> str:
    """Creates the spreadsheet the leads go to and writes its header row, both
    with the tenant's own Google connection. Runs before the transaction, so a
    failure here leaves no rows; a bare ValueError so no provider response
    reaches a log line."""
    try:
        created = await oauth.post_json(tenant_id, connection_id, _SHEETS_API, {
            "properties": {"title": title}, "sheets": [{"properties": {"title": "Sheet1"}}],
        })
        spreadsheet_id = created["spreadsheetId"]
        # RAW here too: the header is ours, but nothing on this sheet is ever parsed as a formula.
        await oauth.post_json(
            tenant_id, connection_id,
            f"{_SHEETS_API}/{quote(spreadsheet_id, safe='')}/values/Sheet1!A1:append?valueInputOption=RAW",
            {"values": [_SHEETS_HEADER]},
        )
    except oauth.ReconnectRequired:
        raise PresetConnectorRequired("connector_required") from None
    except Exception:
        raise ValueError("sheet_create_failed") from None
    return spreadsheet_id


async def _preset_rows(conn, tenant_id: str, preset_key: str) -> list[dict]:
    """The tenant's live rows of one preset, each with its params."""
    rows = [
        custom_apis._decode_custom_api_row(r) for r in await conn.fetch(
            "SELECT * FROM custom_apis WHERE tenant_id = $1 AND preset_key = $2 AND deleted_at IS NULL ORDER BY name",
            tenant_id, preset_key,
        )
    ]
    params = await conn.fetch(
        "SELECT * FROM custom_api_params WHERE custom_api_id = ANY($1::uuid[]) ORDER BY name",
        [r["id"] for r in rows],
    )
    for row in rows:
        row["params"] = custom_apis._redact_sensitive_literals(
            [custom_apis._decode_param_row(p) for p in params if p["custom_api_id"] == row["id"]]
        )
    return rows


async def apply_preset(
    *, tenant_id: str, preset_key: str, setup: BaseModel, user_id: Any, user_email: str | None,
) -> list[dict]:
    """Writes the preset's rows through the same insert path a hand-registered
    API uses, in one advisory-locked transaction. Everything that can fail
    (the connector, the endpoints, the credential, the Sheets calls) happens
    first, so a failure leaves no rows. Applying a preset the tenant already
    has complete is a no-op that returns its rows."""
    preset = PRESETS[preset_key]
    pool = await db.get_pool()

    connection_id = None
    api_base = None
    if preset.provider is not None:
        async with tenant_conn(pool) as conn:
            connection = await oauth.get_connected(conn, tenant_id, preset.provider)
        if connection is None:
            raise PresetConnectorRequired("connector_required")
        if not preset.scopes <= set(connection["scopes"]):
            raise PresetConnectorRequired("connector_scope_required")
        connection_id, api_base = connection["id"], connection["api_base_url"]

    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || $1::text))", str(tenant_id))

            # Everything that can create something external happens under the
            # lock, not before it. Reading `have` outside meant two overlapping
            # applies both saw the row missing and both created a spreadsheet,
            # of which only one ends up referenced — and a Google Sheet is not
            # something a rolled-back transaction takes back. The lock is per
            # tenant and preset apply is a rare console action, so serializing
            # the provider call inside it costs nothing worth having.
            have = {row["name"] for row in await conn.fetch(
                "SELECT name FROM custom_apis WHERE tenant_id = $1 AND preset_key = $2 AND deleted_at IS NULL",
                tenant_id, preset_key,
            )}

            steps_kwargs = {}
            if preset_key == "sheets_lead_capture":
                # Only when the row it belongs to is missing: re-applying must not leave a spreadsheet nobody uses.
                steps_kwargs["spreadsheet_id"] = (
                    "" if "sheets_capture_lead" in have
                    else await _create_lead_sheet(tenant_id, connection_id, setup.title)
                )
            steps = preset.steps(setup, **steps_kwargs)

            for step in steps:
                url = step.endpoint_url
                if step.endpoint_base_source == "oauth_connection":
                    if api_base is None:
                        raise PresetConnectorRequired("connector_required")
                    url = api_base + url
                await resolve_and_validate_endpoint(url)

            secret_ref = None
            if preset_key == "whatsapp_confirmation":
                key = setup.api_key.get_secret_value()
                # Sealed to this tenant: the same ciphertext pasted into another tenant's row cannot be opened.
                secret_ref = encrypt_tenant_secret(
                    tenant_id, "Basic " + key if setup.provider == "interakt" else key
                )
                for step in steps:
                    custom_apis._validate_credential_ref(
                        tenant_id, step.auth_scheme, {_SECRET_REF_FIELD[step.auth_scheme]: secret_ref},
                    )

            created = await _insert_missing_steps(
                conn, tenant_id, preset_key, steps, oauth_connection_id=connection_id, secret_ref=secret_ref,
            )
            if created:
                await custom_apis._recompute_tenant_chain_levels(conn, tenant_id)
            rows = await _preset_rows(conn, tenant_id, preset_key)
            created_ids = {row["id"] for row in created}
            for row in rows:
                if row["id"] in created_ids:
                    await audit.write_audit(
                        conn, entity_type="custom_api", entity_id=row["id"], action="created",
                        user_id=user_id, user_email=user_email, new_value=row,
                    )
    return rows


async def remove_preset(*, tenant_id: str, preset_key: str, user_id: Any, user_email: str | None) -> None:
    """Soft-deletes the tenant's rows of this preset, refusing while a row that
    is not part of it still takes one of them as an upstream."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || $1::text))", str(tenant_id))
            rows = await _preset_rows(conn, tenant_id, preset_key)
            dependent = await conn.fetchrow(
                "SELECT ca.id, ca.name FROM custom_api_params p "
                "JOIN custom_apis ca ON ca.id = p.custom_api_id AND ca.deleted_at IS NULL "
                "WHERE ca.tenant_id = $1 AND ca.preset_key IS DISTINCT FROM $2 "
                "  AND p.upstream_api_id = ANY($3::uuid[]) LIMIT 1",
                tenant_id, preset_key, [r["id"] for r in rows],
            )
            if dependent is not None:
                raise DependentApiExists(
                    f"preset {preset_key} is still declared as an upstream dependency by {dependent['name']!r}"
                )
            await conn.execute(
                "UPDATE custom_apis SET deleted_at = now() "
                "WHERE tenant_id = $1 AND preset_key = $2 AND deleted_at IS NULL",
                tenant_id, preset_key,
            )
            for row in rows:
                await audit.write_audit(
                    conn, entity_type="custom_api", entity_id=row["id"], action="deleted",
                    user_id=user_id, user_email=user_email, old_value=row,
                )
