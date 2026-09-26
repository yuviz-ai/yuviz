"""The mechanical version of the Latency section's claim: the WS route and
the inbound voice/status routes carry zero Depends, the WS handler's own
code makes no call into auth/ownership/idempotency, and a full
connected-call fixture streams through MediaStreamBridge against a Redis
client that raises on every operation."""

from __future__ import annotations

import ast
import audioop
import base64
import inspect
import json

import pytest

from libs.telephony_sdk.providers.fake import FakeProvider
from services.telephony import app as app_module
from services.telephony.accounts import Account, accounts
from services.telephony.app import app


def test_ws_and_inbound_routes_have_no_dependencies():
    targets = {
        ("/{provider}/stream/{token}", frozenset({"WEBSOCKET"})),
        ("/{provider}/voice/{account_ref}", frozenset({"GET", "POST"})),
        ("/{provider}/status/{account_ref}", frozenset({"POST"})),
    }
    checked = 0
    for route in app.routes:
        methods = frozenset(getattr(route, "methods", None) or (["WEBSOCKET"] if hasattr(route, "endpoint") and route.path.endswith("/stream/{token}") else []))
        key = (route.path, methods)
        if route.path in {t[0] for t in targets}:
            checked += 1
            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert dependant.dependencies == [], f"{route.path} has unexpected dependencies"
    assert checked == 3


def test_ws_handler_body_makes_no_call_into_auth_ownership_idempotency():
    source = inspect.getsource(app_module.stream)
    tree = ast.parse(source)
    forbidden_modules = {"auth", "ownership", "idempotency"}
    names_referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            names_referenced.add(node.value.id)
        if isinstance(node, ast.Name):
            names_referenced.add(node.id)
    assert not (names_referenced & forbidden_modules), names_referenced & forbidden_modules


class _FakeWs:
    def __init__(self, frames: list[dict], *, gap_s: float = 0.0):
        self._frames = frames
        self._gap_s = gap_s
        self.sent: list[str] = []
        self.accepted = False
        self.closed_code: int | None = None

    async def accept(self):
        self.accepted = True

    async def iter_text(self):
        import asyncio
        for frame in self._frames:
            if self._gap_s:
                await asyncio.sleep(self._gap_s)
            yield json.dumps(frame)

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class _FakeCall:
    def __init__(self, incoming=None):
        self.written = []
        self._incoming = incoming or []
        self.cancelled = False

    async def write(self, msg) -> None:
        self.written.append(msg)

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        import asyncio
        for msg in self._incoming:
            yield msg
        await asyncio.Event().wait()

    def cancel(self) -> None:
        self.cancelled = True


class _FakeStub:
    def __init__(self, call: _FakeCall):
        self._call = call

    def Converse(self):
        return self._call


class _FakeChannel:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_grpc(monkeypatch, call: _FakeCall) -> None:
    import libs.media_stream_sdk.bridge as bridge_module

    monkeypatch.setattr(bridge_module.grpc.aio, "insecure_channel", lambda target: _FakeChannel())
    monkeypatch.setattr(bridge_module.pb_grpc, "ConversationServiceStub", lambda channel: _FakeStub(call))


class _RaisingRedis:
    async def get(self, *args, **kwargs):
        raise RuntimeError("redis must never be touched on the connected-call path")

    async def set(self, *args, **kwargs):
        raise RuntimeError("redis must never be touched on the connected-call path")


@pytest.mark.asyncio
async def test_connected_call_streams_with_redis_raising_on_every_call(monkeypatch):
    fake_provider = FakeProvider({})
    accounts._accounts = {
        ("fake", "cfg-a"): Account(
            provider="fake", account_ref="cfg-a", tenant_id="t-1", tenant_slug="tenant-a",
            is_default_outbound=True, credentials={}, instance=fake_provider,
        ),
    }
    accounts._loaded = True

    async def _resolve(did: str):
        return ("tenant-a", "sales")

    from services.telephony import orchestrator
    monkeypatch.setattr(orchestrator.did_route, "resolve_did_route", _resolve)

    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/fake/voice/cfg-a",
        "headers": [(b"x-fake-signature", b"valid")],
        "query_string": b"call_id=call-1&from=1000&to=2000",
        "server": ("test", 80), "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    resp = await orchestrator.handle_inbound_webhook(
        provider_name="fake", account_ref="cfg-a", request=Request(scope, receive),
    )
    assert resp.status_code == 200
    token = resp.body.decode().rsplit("/fake/stream/", 1)[1]

    from libs.media_stream_sdk.serializers import CloudonixSerializer
    monkeypatch.setitem(app_module._SERIALIZERS, "fake", CloudonixSerializer)

    call = _FakeCall(incoming=[])
    _patch_grpc(monkeypatch, call)

    # Only NOW install the raising Redis client — the webhook phase above
    # legitimately performs a did: GET and must run before this.
    import libs.telephony_sdk.did_route as real_did_route
    monkeypatch.setattr(real_did_route, "_get_client", lambda: _RaisingRedis())
    import services.telephony.idempotency as idem_module
    monkeypatch.setattr(idem_module, "_get_client", lambda: _RaisingRedis())

    pcm_8k = (b"\x10\x20" * 80)
    ulaw = audioop.lin2ulaw(pcm_8k, 2)
    payload_b64 = base64.b64encode(ulaw).decode("ascii")
    ws = _FakeWs([
        {"event": "start", "start": {"streamSid": "MZ1"}},
        {"event": "media", "media": {"payload": payload_b64}},
        {"event": "stop"},
    ], gap_s=0.25)

    await app_module.stream(ws, "fake", token)

    assert ws.closed_code is None
    audio_msgs = [m for m in call.written if m.WhichOneof("payload") == "audio_chunk"]
    assert audio_msgs, "expected at least one AudioChunk to have streamed"

    accounts._accounts = {}
    accounts._loaded = False
