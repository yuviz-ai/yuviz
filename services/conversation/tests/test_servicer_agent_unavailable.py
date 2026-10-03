"""Servicer handling of a refused test session (guided agent creation, T12)."""
import logging
from unittest.mock import AsyncMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer
from services.conversation.session import AgentUnavailable

SENTINEL = "sentinel-credential-7f3a9c"


async def _stream(*messages):
    for m in messages:
        yield m


def _open(credential: str = SENTINEL) -> pb.GatewayMessage:
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="t1",
        direction="test", test_credential=credential,
    ))


async def _converse(factory, caplog) -> list[pb.ServiceMessage]:
    servicer = ConversationServicer(factory)
    with caplog.at_level(logging.DEBUG):
        return [m async for m in servicer.Converse(_stream(_open()), AsyncMock())]


async def test_agent_unavailable_yields_one_fatal_error_and_ends(caplog):
    async def refuse(ctx):
        raise AgentUnavailable()

    out = await _converse(refuse, caplog)

    assert len(out) == 1
    err = out[0].error
    assert out[0].WhichOneof("payload") == "error"
    assert (err.code, err.message, err.fatal) == ("AGENT_UNAVAILABLE", "agent unavailable", True)
    assert err.session_id == "s1"


async def test_open_log_never_contains_credential(caplog):
    seen = []

    async def refuse(ctx):
        seen.append(ctx.test_credential)
        raise AgentUnavailable()

    await _converse(refuse, caplog)

    assert seen == [SENTINEL]  # the credential did reach the factory
    assert caplog.records
    assert SENTINEL not in caplog.text


async def test_non_test_session_still_opens(caplog):
    handler = AsyncMock()
    handler.greeting.return_value = []

    async def factory(ctx):
        return handler

    responses = ConversationServicer(factory).Converse(_stream(_open("")), AsyncMock())
    first = await anext(responses)
    await responses.aclose()

    assert first.WhichOneof("payload") == "service_ready"
