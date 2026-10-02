"""
Place outbound calls via raw FreeSWITCH ESL; the answered leg bridges back
through Kamailio to the agent's DID, taking the same path as inbound calls.

ESL command shape:
    bgapi originate {origination_caller_id_number=<caller_id>}sofia/external/sip:<phone_number>@<sip_proxy_host>:<sip_proxy_port> &bridge(sofia/external/sip:<caller_id>@<sip_proxy_host>:<sip_proxy_port>)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

log = logging.getLogger(__name__)

# Digits only (optional leading +): anything else interpolated into the ESL
# command below could smuggle a newline and a second command.
_DIAL_NUMBER_RE = re.compile(r"\+?[0-9]{3,15}")

_ESL_HOST = os.environ.get("FREESWITCH_ESL_HOST", "127.0.0.1")
_ESL_PORT = int(os.environ.get("FREESWITCH_ESL_PORT", "8022"))
# No default: FreeSWITCH's own default is public. Set it in .env.
_ESL_PASSWORD = os.environ.get("FREESWITCH_ESL_PASSWORD", "")
# No default: Kamailio's IP is host-specific (scripts/update_kamailio_ip.sh writes it).
_SIP_PROXY_HOST = os.environ.get("SIP_PROXY_HOST", "")
_SIP_PROXY_PORT = int(os.environ.get("SIP_PROXY_PORT", "5060"))


class OriginateError(Exception):
    """ESL-level failure (connect, auth, command rejected) — never raised for no-answer."""


def is_valid_dial_number(value: str) -> bool:
    return _DIAL_NUMBER_RE.fullmatch(value) is not None


async def _read_until_blank_line(reader: asyncio.StreamReader) -> dict[str, str]:
    """Read one ESL 'Header: value' block terminated by a blank line."""
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\n", b"\r\n"):
            break
        decoded = line.decode(errors="replace").rstrip("\r\n")
        if ":" in decoded:
            key, _, value = decoded.partition(":")
            headers[key.strip()] = value.strip()
    return headers


async def originate_call(phone_number: str, caller_id: str) -> str:
    """Dial phone_number and bridge it to caller_id (agent DID); returns the bgapi Job-UUID.

    Only reports acceptance; the call outcome arrives later as a BACKGROUND_JOB event."""
    for label, value in (("phone_number", phone_number), ("caller_id", caller_id)):
        if not is_valid_dial_number(value):
            raise OriginateError(f"refusing to dial: {label} {value!r} is not a plain dial number")
    if not _ESL_PASSWORD:
        raise OriginateError("FREESWITCH_ESL_PASSWORD is not set; add it to .env")
    if not _SIP_PROXY_HOST:
        raise OriginateError(
            "SIP_PROXY_HOST is not set; run scripts/update_kamailio_ip.sh to write "
            "Kamailio's IP into .env, then restart Campaigns"
        )
    try:
        reader, writer = await asyncio.open_connection(_ESL_HOST, _ESL_PORT)
    except OSError as exc:
        raise OriginateError(f"cannot reach FreeSWITCH ESL at {_ESL_HOST}:{_ESL_PORT}: {exc}") from exc

    try:
        # FreeSWITCH sends auth/request unprompted on connect.
        await _read_until_blank_line(reader)
        writer.write(f"auth {_ESL_PASSWORD}\n\n".encode())
        await writer.drain()
        auth_reply = await _read_until_blank_line(reader)
        if auth_reply.get("Reply-Text", "").strip() != "+OK accepted":
            raise OriginateError(f"ESL auth rejected: {auth_reply}")

        dial_string = f"sofia/external/sip:{phone_number}@{_SIP_PROXY_HOST}:{_SIP_PROXY_PORT}"
        # Bridge via Kamailio, not `<exten> XML <context>`: Kamailio only routes
        # a DID to the AI pipeline when it's the Request-URI of a new INVITE.
        bridge_target = f"sofia/external/sip:{caller_id}@{_SIP_PROXY_HOST}:{_SIP_PROXY_PORT}"
        command = (
            f"bgapi originate {{origination_caller_id_number={caller_id}}}"
            f"{dial_string} &bridge({bridge_target})"
        )
        # Backstop: a line break ends an ESL command, so one here would run a second command.
        if any(c in command for c in "\r\n\0"):
            raise OriginateError("refusing to send an ESL command containing CR/LF/NUL")
        writer.write(f"{command}\n\n".encode())
        await writer.drain()
        reply = await _read_until_blank_line(reader)

        reply_text = reply.get("Reply-Text", "")
        if not reply_text.startswith("+OK"):
            raise OriginateError(f"originate rejected: {reply}")

        # +OK Job-UUID: <uuid>
        job_uuid = reply_text.split("Job-UUID:")[-1].strip() if "Job-UUID:" in reply_text else ""
        log.info(
            "originate_call: accepted phone_number=%s caller_id=%s job_uuid=%s",
            phone_number, caller_id, job_uuid,
        )
        return job_uuid
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


class EslJobEventListener:
    """Persistent ESL event connection subscribed to BACKGROUND_JOB (separate from command conns)."""

    def __init__(self, on_job_complete) -> None:
        # on_job_complete(job_uuid: str, succeeded: bool, detail: str) -> Awaitable[None]
        self._on_job_complete = on_job_complete
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        if not _ESL_PASSWORD:
            log.error("EslJobEventListener: FREESWITCH_ESL_PASSWORD is not set; not listening for call outcomes")
            return
        while not self._stopped:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("EslJobEventListener: connection lost, reconnecting in 5s")
                await asyncio.sleep(5)

    async def _listen_once(self) -> None:
        reader, writer = await asyncio.open_connection(_ESL_HOST, _ESL_PORT)
        try:
            await _read_until_blank_line(reader)
            writer.write(f"auth {_ESL_PASSWORD}\n\n".encode())
            await writer.drain()
            await _read_until_blank_line(reader)

            writer.write(b"event plain BACKGROUND_JOB\n\n")
            await writer.drain()
            await _read_until_blank_line(reader)  # command/reply ack for the event subscription itself

            log.info("EslJobEventListener: subscribed to BACKGROUND_JOB events")
            while not self._stopped:
                headers = await _read_until_blank_line(reader)
                if not headers:
                    continue
                content_length = int(headers.get("Content-Length", "0"))
                body = (await reader.readexactly(content_length)).decode(errors="replace") if content_length else ""
                job_uuid, succeeded, detail = _parse_job_event(headers, body)
                if job_uuid:
                    await self._on_job_complete(job_uuid, succeeded, detail)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def _parse_job_event(headers: dict[str, str], body: str) -> tuple[str, bool, str]:
    """Parse the body's inner header block (Job-UUID lives there, not in the
    outer envelope) and the job's +OK/-ERR reply text after it."""
    del headers
    header_part, sep, reply_part = body.partition("\r\n\r\n")
    if not sep:
        header_part, sep, reply_part = body.partition("\n\n")

    event_headers: dict[str, str] = {}
    for line in header_part.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            event_headers[key.strip()] = value.strip()

    job_uuid = event_headers.get("Job-UUID", "")
    reply_text = reply_part.strip()
    succeeded = reply_text.startswith("+OK")
    return job_uuid, succeeded, reply_text
