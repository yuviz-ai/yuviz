from __future__ import annotations

import json
import pathlib
import uuid

import httpx
import pytest

from services.campaigns import dial_request
from services.campaigns.dial_request import (
    DialIntent, DialRequestClient, InvalidDialField, build_intent, canonical_body, egress_token, verify_egress_token,
)

VECTORS = pathlib.Path(__file__).parent / "vectors"
TOKEN_VECTORS = json.loads((VECTORS / "egress_token.json").read_text())
BODY_VECTORS = json.loads((VECTORS / "dial_body.json").read_text())

KEY = "7d0b4d0e-0f2c-4a9f-9a3e-5d2c1b0a9e8f_1"
SECRET = b"dial-test-secret"


def _intent(**overrides) -> DialIntent:
    fields = dict(
        key=KEY, tenant_id=str(uuid.uuid4()), campaign_id=str(uuid.uuid4()), contact_id=str(uuid.uuid4()),
        call_uuid=str(uuid.uuid4()), callee="+919876543210", caller_id="+918065354620", tenant_slug="yuviz", agent_slug="sales",
        max_call_s=900, egress_secret=b"egress", now=1_900_000_000,
    )
    fields.update(overrides)
    return build_intent(**fields)


# ── egress token and intent ──

@pytest.mark.parametrize("vec", TOKEN_VECTORS)
def test_python_mint_matches_shared_vectors(vec):
    minted = egress_token(
        key=vec["key"], callee=vec["callee"], caller_id=vec["caller_id"], tenant_id=vec["tenant_id"],
        max_call_s=vec["max_call_s"], exp=vec["exp"], secret=vec["secret"].encode(),
    )
    assert minted == vec["token"]
    assert verify_egress_token(minted, callee=vec["callee"], secret=vec["secret"].encode(), now=vec["exp"] - 1)


@pytest.mark.parametrize("field_index, replacement", [
    (0, "7d0b4d0e-0f2c-4a9f-9a3e-5d2c1b0a9e8f_2"),   # key
    (1, "1900000121"),                                # exp
    (2, "0b6f1c8e-1111-4222-8333-444455556667"),      # tenant_id
    (3, "+918065354621"),                             # caller_id
    (4, "7201"),                                      # max_call_s
])
def test_changing_any_token_field_fails_verification(field_index, replacement):
    vec = TOKEN_VECTORS[0]
    parts = vec["token"].split(".")
    parts[field_index] = replacement
    assert not verify_egress_token(".".join(parts), callee=vec["callee"], secret=vec["secret"].encode(), now=0)


def test_changing_callee_secret_or_mac_fails_verification():
    vec = TOKEN_VECTORS[0]
    secret = vec["secret"].encode()
    assert verify_egress_token(vec["token"], callee=vec["callee"], secret=secret, now=0)
    assert not verify_egress_token(vec["token"], callee="+919876543211", secret=secret, now=0)
    assert not verify_egress_token(vec["token"], callee=vec["callee"], secret=b"other", now=0)
    assert not verify_egress_token(vec["token"] + "x", callee=vec["callee"], secret=secret, now=0)
    assert not verify_egress_token(vec["token"], callee=vec["callee"], secret=secret, now=vec["exp"] + 1)


def test_token_exp_is_intent_exp_plus_120():
    intent = _intent()
    assert intent.exp == 1_900_000_060
    assert intent.egress_token.split(".")[1] == str(intent.exp + 120)
    assert verify_egress_token(intent.egress_token, callee=intent.callee, secret=b"egress", now=intent.exp + 119)


@pytest.mark.parametrize("field, value", [("key", "a.b"), ("tenant_id", "a.b"), ("caller_id", "+9180.65"), ("max_call_s", "9.9")])
def test_dotted_token_field_raises(field, value):
    args = dict(key=KEY, callee="+919876543210", caller_id="+918065354620", tenant_id="t", max_call_s=900, exp=1, secret=b"s")
    args[field] = value
    with pytest.raises(InvalidDialField):
        egress_token(**args)


@pytest.mark.parametrize("bad", [None, 0, 9, 10000, "9", "abc", 7.5, True])
def test_intent_rejects_bad_max_call_s(bad):
    with pytest.raises(InvalidDialField):
        _intent(max_call_s=bad)


def test_intent_requires_max_call_s():
    fields = {n: "x" for n in DialIntent.__dataclass_fields__ if n != "max_call_s"}
    with pytest.raises(TypeError):
        DialIntent(**fields)


@pytest.mark.parametrize("good", [30, 99, 900, 7200, 9999])
def test_intent_accepts_in_range_max_call_s(good):
    assert _intent(max_call_s=good).max_call_s == good


# Carried over from test_originate.py: nothing but plain numbers may reach a header, body or originate string.
_INJECTION_SHAPES = [
    "5551234\n\napi hupall", "5551234\r\n\r\napi hupall", "+14155551234\n", "+14155551234\x00", "5551234@evil.example",
    "+1 4155551234", "1001&bridge(sofia/external/sip:1002@x)", "+１２３４５６７８９０", "12", "+1234567890123456", "",
    "14155551234",
]


@pytest.mark.parametrize("bad", _INJECTION_SHAPES)
@pytest.mark.parametrize("field", ["callee", "caller_id"])
def test_number_fields_reject_injection_shapes(field, bad):
    with pytest.raises(InvalidDialField):
        _intent(**{field: bad})


@pytest.mark.parametrize("bad", ["x\n", "x\x00", KEY + "\r\n", "../etc", ""])
@pytest.mark.parametrize("field", ["key", "tenant_id", "campaign_id", "contact_id", "call_uuid"])
def test_identifier_fields_reject_injection_shapes(field, bad):
    with pytest.raises(InvalidDialField):
        _intent(**{field: bad})


@pytest.mark.parametrize("bad", ["x\n", "Yuviz", "a b", "-a", "a" * 64, "", "a/../b", "a\"b"])
@pytest.mark.parametrize("field", ["tenant_slug", "agent_slug"])
def test_slug_fields_reject_injection_shapes(field, bad):
    with pytest.raises(InvalidDialField):
        _intent(**{field: bad})


# ── client ──

class _Kamailio:
    def __init__(self, status: int | Exception = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.status, Exception):
            raise self.status
        return httpx.Response(self.status, json={"result": "x"})


def _client(redis_client, kamailio: _Kamailio) -> DialRequestClient:
    client = DialRequestClient("http://127.0.0.1:5071/campaign-dial", SECRET, redis_client)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(kamailio.handler))
    return client


@pytest.fixture(autouse=True)
async def _flush(scratch_redis):
    await scratch_redis.flushall()


@pytest.mark.parametrize("status, expected, intent_kept", [
    (200, "accepted", True),
    (400, "rejected", False), (401, "rejected", False), (403, "rejected", False),
    (404, "rejected", False), (405, "rejected", False),
    (503, "no_node", False),
    (500, "timeout", True), (502, "timeout", True),
    (httpx.ConnectTimeout("t"), "timeout", True), (httpx.ConnectError("c"), "timeout", True),
])
async def test_send_result_table(scratch_redis, status, expected, intent_kept):
    kamailio = _Kamailio(status)
    client = _client(scratch_redis, kamailio)
    intent = _intent()
    assert await client.send(intent, first=True) == expected
    assert len(kamailio.requests) == 1
    assert bool(await scratch_redis.exists(f"campaign:intent:{intent.key}")) is intent_kept
    await client.close()


async def test_first_send_stores_the_intent_with_a_ttl_and_the_time_limit(scratch_redis):
    client = _client(scratch_redis, _Kamailio())
    intent = _intent(max_call_s=900)
    await client.send(intent, first=True)
    stored = json.loads(await scratch_redis.get(f"campaign:intent:{intent.key}"))
    assert stored["max_call_s"] == 900 and stored["callee"] == intent.callee and stored["egress_token"] == intent.egress_token
    assert 0 < await scratch_redis.ttl(f"campaign:intent:{intent.key}") <= dial_request.INTENT_TTL_S
    await client.close()


async def test_second_first_send_is_a_duplicate_with_no_http_request(scratch_redis):
    kamailio = _Kamailio()
    client = _client(scratch_redis, kamailio)
    intent = _intent()
    assert await client.send(intent, first=True) == "accepted"
    assert await client.send(intent, first=True) == "duplicate"
    assert len(kamailio.requests) == 1


async def test_resend_after_the_intent_was_consumed_is_a_duplicate(scratch_redis):
    kamailio = _Kamailio()
    client = _client(scratch_redis, kamailio)
    intent = _intent()
    await client.send(intent, first=True)
    await scratch_redis.getdel(f"campaign:intent:{intent.key}")
    assert await client.send(intent, first=False) == "duplicate"
    assert len(kamailio.requests) == 1


async def test_resend_with_an_existing_claim_is_a_duplicate(scratch_redis):
    kamailio = _Kamailio()
    client = _client(scratch_redis, kamailio)
    intent = _intent()
    await client.send(intent, first=True)
    await scratch_redis.set(f"campaign:claim:{intent.key}", "uuid|host")
    assert await client.send(intent, first=False) == "duplicate"
    assert len(kamailio.requests) == 1


async def test_resend_of_an_unconsumed_intent_posts_again(scratch_redis):
    kamailio = _Kamailio()
    client = _client(scratch_redis, kamailio)
    intent = _intent()
    await client.send(intent, first=True)
    assert await client.send(intent, first=False) == "accepted"
    assert len(kamailio.requests) == 2


@pytest.mark.parametrize("vec", BODY_VECTORS)
async def test_body_and_signature_match_the_shared_vector(scratch_redis, monkeypatch, vec):
    kamailio = _Kamailio()
    client = DialRequestClient("http://127.0.0.1:5071/campaign-dial", vec["secret"].encode(), scratch_redis)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(kamailio.handler))
    monkeypatch.setattr(dial_request.time, "time", lambda: vec["ts"])
    intent = _intent(
        key=vec["key"], campaign_id=vec["campaign_id"], contact_id=vec["contact_id"], callee=vec["callee"],
        caller_id=vec["caller_id"], tenant_slug=vec["tenant_slug"], agent_slug=vec["agent_slug"],
    )
    assert canonical_body(intent, vec["ts"]).decode() == vec["body"]
    await client.send(intent, first=True)
    [request] = kamailio.requests
    assert request.content.decode() == vec["body"]
    assert request.headers["X-Campaign-Signature"] == vec["signature"]
    await client.close()


async def test_probe_posts_a_signed_health_request(scratch_redis):
    kamailio = _Kamailio(200)
    client = _client(scratch_redis, kamailio)
    assert await client.probe() is True
    [request] = kamailio.requests
    assert request.url.path == "/campaign-dial/health" and json.loads(request.content).keys() == {"ts"}
    assert request.headers["X-Campaign-Signature"] == dial_request._mac(SECRET, request.content)
    kamailio.status = 503
    assert await client.probe() is False
    kamailio.status = httpx.ConnectError("down")
    assert await client.probe() is False
    assert not await scratch_redis.keys("campaign:*")


async def test_close_closes_the_http_client(scratch_redis):
    client = DialRequestClient("http://127.0.0.1:5071/campaign-dial", SECRET, scratch_redis)
    assert not client._http.is_closed
    await client.close()
    assert client._http.is_closed
