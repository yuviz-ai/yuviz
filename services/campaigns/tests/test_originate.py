"""originate.py tests against a fake local ESL server; no real FreeSWITCH."""

from __future__ import annotations

import asyncio

import pytest

from services.campaigns import originate


class _FakeEslServer:
    """Minimal ESL auth + bgapi originate exchange with a configurable reply."""

    def __init__(self, originate_reply: str, auth_ok: bool = True) -> None:
        self.originate_reply = originate_reply
        self.auth_ok = auth_ok
        self.received_commands: list[str] = []
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"Content-Type: auth/request\n\n")
        await writer.drain()

        auth_line = await reader.readline()
        self.received_commands.append(auth_line.decode().strip())
        await reader.readline()  # blank line terminator
        if self.auth_ok:
            writer.write(b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n")
        else:
            writer.write(b"Content-Type: command/reply\nReply-Text: -ERR invalid\n\n")
        await writer.drain()
        if not self.auth_ok:
            writer.close()
            return

        originate_line = await reader.readline()
        self.received_commands.append(originate_line.decode().strip())
        await reader.readline()
        writer.write(f"Content-Type: command/reply\nReply-Text: {self.originate_reply}\n\n".encode())
        await writer.drain()
        writer.close()


@pytest.fixture(autouse=True)
def _point_at_fake_server(monkeypatch):
    """Ensure no test reaches a real ESL endpoint; tests set _ESL_PORT themselves."""
    monkeypatch.setattr(originate, "_ESL_HOST", "127.0.0.1")
    monkeypatch.setattr(originate, "_ESL_PASSWORD", "test-esl-password")
    monkeypatch.setattr(originate, "_SIP_PROXY_HOST", "192.168.0.116")


async def test_originate_call_success_returns_job_uuid(monkeypatch):
    server = _FakeEslServer("+OK Job-UUID: abc-123-def")
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)

    job_uuid = await originate.originate_call("+14155551234", "+14155550100")

    assert job_uuid == "abc-123-def"
    assert any("auth" in c for c in server.received_commands)
    assert any("bgapi originate" in c for c in server.received_commands)
    assert any("+14155551234" in c for c in server.received_commands)
    await server.stop()


async def test_originate_call_auth_rejected_raises(monkeypatch):
    server = _FakeEslServer("+OK", auth_ok=False)
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)

    with pytest.raises(originate.OriginateError, match="auth"):
        await originate.originate_call("+14155551234", "+14155550100")

    await server.stop()


async def test_originate_call_command_rejected_raises(monkeypatch):
    server = _FakeEslServer("-ERR DESTINATION_OUT_OF_ORDER")
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)

    with pytest.raises(originate.OriginateError, match="rejected"):
        await originate.originate_call("+14155551234", "+14155550100")

    await server.stop()


async def test_originate_call_no_server_listening_raises(monkeypatch):
    monkeypatch.setattr(originate, "_ESL_PORT", 1)  # nothing listens on port 1

    with pytest.raises(originate.OriginateError, match="cannot reach"):
        await originate.originate_call("+14155551234", "+14155550100")


_INJECTION_SHAPES = [
    "5551234\n\napi hupall",
    "5551234\r\n\r\napi hupall",
    "1234\n",
    "5551234@evil.example",
    "+1 4155551234",
    "1001&bridge(sofia/external/sip:1002@x)",
    "１２３４",  # full-width digits
    "12",
    "1234567890123456",
    "",
]


@pytest.mark.parametrize("bad", _INJECTION_SHAPES)
@pytest.mark.parametrize("field", ["phone_number", "caller_id"])
async def test_originate_call_rejects_non_digit_input_before_connecting(monkeypatch, bad, field):
    server = _FakeEslServer("+OK Job-UUID: abc")
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)
    args = {"phone_number": "+14155551234", "caller_id": "5006", field: bad}

    with pytest.raises(originate.OriginateError, match="refusing to dial"):
        await originate.originate_call(**args)

    assert server.received_commands == []
    await server.stop()


@pytest.mark.parametrize("good", ["1001", "5006", "+14155551234", "919876543210"])
def test_is_valid_dial_number_accepts_plain_numbers(good):
    assert originate.is_valid_dial_number(good)


async def test_originate_call_never_sends_a_command_containing_a_line_break(monkeypatch):
    server = _FakeEslServer("+OK Job-UUID: abc")
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)
    monkeypatch.setattr(originate, "_SIP_PROXY_HOST", "proxy\n\napi hupall")

    with pytest.raises(originate.OriginateError, match="CR/LF"):
        await originate.originate_call("+14155551234", "5006")

    assert not any("originate" in c or "hupall" in c for c in server.received_commands)
    await server.stop()


def test_parse_job_event_success():
    # Job-UUID lives in the body's inner header block, not the outer envelope.
    body = "Event-Name: BACKGROUND_JOB\nJob-UUID: abc-123\nContent-Length: 20\n\n+OK channel-uuid-xyz"
    job_uuid, succeeded, detail = originate._parse_job_event(
        {"Content-Type": "text/event-plain", "Content-Length": "80"}, body,
    )
    assert job_uuid == "abc-123"
    assert succeeded is True


def test_parse_job_event_failure():
    body = "Event-Name: BACKGROUND_JOB\nJob-UUID: abc-123\nContent-Length: 15\n\n-ERR NO_ANSWER"
    job_uuid, succeeded, detail = originate._parse_job_event(
        {"Content-Type": "text/event-plain", "Content-Length": "70"}, body,
    )
    assert job_uuid == "abc-123"
    assert succeeded is False
    assert "NO_ANSWER" in detail


async def test_originate_refuses_without_an_esl_password(monkeypatch):
    monkeypatch.setattr(originate, "_ESL_PASSWORD", "")
    with pytest.raises(originate.OriginateError, match="FREESWITCH_ESL_PASSWORD"):
        await originate.originate_call("+14155551111", "+14155552222")


async def test_originate_refuses_without_a_sip_proxy_host_before_connecting(monkeypatch):
    server = _FakeEslServer("+OK Job-UUID: abc")
    port = await server.start()
    monkeypatch.setattr(originate, "_ESL_PORT", port)
    monkeypatch.setattr(originate, "_SIP_PROXY_HOST", "")
    with pytest.raises(originate.OriginateError, match="SIP_PROXY_HOST is not set"):
        await originate.originate_call("+14155551111", "+14155552222")
    assert server.received_commands == []
    await server.stop()

