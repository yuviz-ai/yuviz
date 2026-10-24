"""Raw FreeSWITCH ESL helpers for clinic reminder calls: dial-number check, header reader, BACKGROUND_JOB listener."""

from __future__ import annotations

import asyncio
import logging
import os
import re

log = logging.getLogger(__name__)

# Digits only (optional leading +): anything else interpolated into an ESL
# command could smuggle a newline and a second command.
_DIAL_NUMBER_RE = re.compile(r"\+?[0-9]{3,15}")

_ESL_HOST = os.environ.get("FREESWITCH_ESL_HOST", "127.0.0.1")
_ESL_PORT = int(os.environ.get("FREESWITCH_ESL_PORT", "8022"))
# No default: FreeSWITCH's own default is public. Set it in .env.
_ESL_PASSWORD = os.environ.get("FREESWITCH_ESL_PASSWORD", "")


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
