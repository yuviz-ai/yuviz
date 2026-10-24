"""Consumes the per-producer call-event streams and applies them to attempts.

The source of an event is the stream it was read from, never a field in the entry, so one producer cannot write
another's events. Entries are acked only after Postgres commits; an entry left un-acked by a dead consumer is
reclaimed once idle. The Redis client must not decode responses (campaign_events.decode wants bytes)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import redis.asyncio as redis

from libs.telephony_sdk import campaign_events
from libs.telephony_sdk.campaign_events import CampaignCallEvent, GROUP, STREAM_CONV, STREAM_CPAAS, STREAM_FS

from . import attempts, telephony_originate

log = logging.getLogger(__name__)

RECLAIM_IDLE_MS = 60_000
BATCH = 100

# Per stream: the event kinds it may carry. Anything else is dropped.
_KINDS_BY_STREAM = {
    STREAM_FS: {"answered", "hangup"},
    STREAM_CPAAS: {"answered", "hangup"},
    STREAM_CONV: set(),
}
_CONV_FIRST = {STREAM_CONV: 0, STREAM_FS: 1, STREAM_CPAAS: 1}

_FS_CAUSE_OUTCOME = {
    "USER_BUSY": "busy", "NO_ANSWER": "no_answer", "NO_USER_RESPONSE": "no_answer", "ALLOTTED_TIMEOUT": "no_answer",
    "CALL_REJECTED": "declined",
}
_TERMINAL_API_STATUS = {"ended", "busy", "no_answer", "declined", "failed"}


async def ensure_groups(client: redis.Redis) -> None:
    for stream in (STREAM_FS, STREAM_CPAAS, STREAM_CONV):
        try:
            await client.xgroup_create(stream, GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise


async def drain(client: redis.Redis, consumer: str) -> int:
    """Handle one batch of new and reclaimed entries; returns how many were acked."""
    entries: list[tuple[str, str, dict[bytes, bytes]]] = []
    for stream in (STREAM_FS, STREAM_CPAAS, STREAM_CONV):
        _, claimed, _ = await client.xautoclaim(stream, GROUP, consumer, RECLAIM_IDLE_MS, "0-0", count=BATCH)
        entries += [(stream, _text(entry_id), fields) for entry_id, fields in claimed if fields]
    fresh = await client.xreadgroup(
        GROUP, consumer, {s: ">" for s in (STREAM_FS, STREAM_CPAAS, STREAM_CONV)}, count=BATCH,
    )
    for stream, items in fresh or []:
        entries += [(_text(stream), _text(entry_id), fields) for entry_id, fields in items]

    entries.sort(key=lambda entry: _CONV_FIRST[entry[0]])
    for stream, entry_id, fields in entries:
        await _handle(stream, fields)
        await client.xack(stream, GROUP, entry_id)
    return len(entries)


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def _handle(stream: str, fields: dict[bytes, bytes]) -> None:
    try:
        event = campaign_events.decode(fields)
    except ValueError:
        log.warning("campaign event with an unknown kind dropped stream=%s", stream)
        return
    if event.kind not in _KINDS_BY_STREAM[stream]:
        log.warning("campaign event kind=%s not allowed on stream=%s, dropped", event.kind, stream)
        return
    tenant_id = _tenant(event)
    if tenant_id is None or event.key is None:
        log.warning("campaign event without a valid tenant and key dropped stream=%s", stream)
        return

    source = campaign_events.source_for_stream(stream)
    if source == "fs":
        applied = await _apply_fs(tenant_id, event)
    elif event.kind == "answered":
        applied = await attempts.apply_outcome(
            tenant_id=tenant_id, key=event.key, provider_call_id=event.provider_call_id, outcome="answered",
            call_session_id=event.call_session_id,
        )
    else:
        applied = await _apply_cpaas_hangup(tenant_id, event)
    if applied is None:
        log.debug("campaign event changed no attempt stream=%s key=%s", stream, event.key)


def _tenant(event: CampaignCallEvent) -> uuid.UUID | None:
    try:
        return uuid.UUID(event.tenant_id) if event.tenant_id else None
    except ValueError:
        return None


async def _apply_fs(tenant_id: uuid.UUID, event: CampaignCallEvent) -> dict[str, Any] | None:
    if event.kind == "answered":
        outcome = "answered"
    elif event.answered:
        outcome = "ended"
    else:
        outcome = _FS_CAUSE_OUTCOME.get(event.cause, "failed")
    return await attempts.apply_outcome(
        tenant_id=tenant_id, key=event.key, provider_call_id=None, outcome=outcome,
        call_session_id=event.call_session_id,
    )


async def _apply_cpaas_hangup(tenant_id: uuid.UUID, event: CampaignCallEvent) -> dict[str, Any] | None:
    """The webhook only says a call ended; the outcome is whatever the provider's own API reports."""
    attempt = await attempts.get_attempt(tenant_id, event.key)
    if attempt is None or attempt["path"] != "cpaas":
        return None
    call_id = attempt["provider_call_id"] or event.provider_call_id
    if call_id is None:
        return None
    try:
        status = await telephony_originate.call_status(
            provider=attempt["provider"], tenant_slug=attempt["tenant_slug"], call_id=call_id,
        )
    except telephony_originate.TelephonyOriginateError:
        log.warning("provider status lookup failed, leaving the attempt to its deadline key=%s", event.key)
        return None
    if status["status"] not in _TERMINAL_API_STATUS:
        return None
    if attempt["provider_call_id"] is None and (
        status.get("to") != attempt["callee"] or status.get("from") != attempt["caller_id"]
    ):
        log.warning("provider call does not match the attempt, ignored key=%s", event.key)
        return None
    return await attempts.apply_outcome(
        tenant_id=tenant_id, key=event.key, provider_call_id=call_id, outcome=status["status"], call_session_id=None,
    )
