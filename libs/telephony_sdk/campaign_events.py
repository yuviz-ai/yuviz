"""Campaign call-event schema shared by the producers (FreeSWITCH Lua, Telephony, Conversation) and the
Campaigns stream consumer. One stream per producer: the consumer takes the source from the stream it read,
never from the entry's own fields, so a producer cannot write another producer's events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

STREAM_FS = "campaign:events:fs"
STREAM_CPAAS = "campaign:events:cpaas"
STREAM_CONV = "campaign:events:conv"
GROUP = "campaigns"
# Telephony's replay guard; its ACL user may only SET this prefix.
WEBHOOK_NONCE_KEY = "telephony:webhook_nonce:{provider}:{account_ref}:{nonce}"

Source = Literal["fs", "cpaas"]
Kind = Literal["answered", "hangup", "dnc_request"]
_KINDS = ("answered", "hangup", "dnc_request")
_SOURCE_BY_STREAM: dict[str, Source] = {STREAM_FS: "fs", STREAM_CPAAS: "cpaas"}


@dataclass(frozen=True)
class CampaignCallEvent:
    kind: Kind
    key: str | None
    tenant_id: str | None
    provider_call_id: str | None
    call_session_id: str | None
    cause: str
    answered: bool
    billable_ms: int | None
    unconfirmed: bool = False  # the sender's signature does not cover the entry's fields: key and status are hints


def source_for_stream(name: str) -> Source | None:
    """`fs`/`cpaas` only for their own stream; the conv stream carries no call outcomes, so None."""
    return _SOURCE_BY_STREAM.get(name)


def encode(
    *, source: Source | Literal["conv"], kind: Kind, key: str | None, tenant_id: str | None,
    provider_call_id: str | None, call_session_id: str | None, cause: str, answered: bool,
    billable_ms: int | None, unconfirmed: bool = False,
) -> dict[str, str]:
    """Entry fields for XADD; None values are omitted. A `dnc_request` carries only `call_session_id`."""
    fields = {
        "source": source, "kind": kind, "key": key, "tenant_id": tenant_id,
        "provider_call_id": provider_call_id, "call_session_id": call_session_id,
        "cause": cause, "answered": "1" if answered else "0",
        "billable_ms": None if billable_ms is None else str(billable_ms),
        "unconfirmed": "1" if unconfirmed else None,
    }
    return {k: v for k, v in fields.items() if v is not None}


def decode(fields: dict[bytes, bytes]) -> CampaignCallEvent:
    """Parse an XREADGROUP entry. The entry's `source` field is deliberately not exposed (see module doc)."""
    text = {k.decode(): v.decode() for k, v in fields.items()}
    kind = text.get("kind")
    if kind not in _KINDS:
        raise ValueError(f"unknown campaign event kind: {kind!r}")
    billable_ms = text.get("billable_ms")
    return CampaignCallEvent(
        kind=kind,
        key=text.get("key"),
        tenant_id=text.get("tenant_id"),
        provider_call_id=text.get("provider_call_id"),
        call_session_id=text.get("call_session_id"),
        cause=text.get("cause", ""),
        answered=text.get("answered") == "1",
        billable_ms=None if billable_ms is None else int(billable_ms),
        unconfirmed=text.get("unconfirmed") == "1",
    )
