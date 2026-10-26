import asyncio
import urllib.request

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from services.webcall.__main__ import _health


async def _echo(ws):
    await ws.send(await ws.recv())


async def test_health_answers_plain_http_and_websockets_still_work():
    async with serve(_echo, "127.0.0.1", 0, process_request=_health) as server:
        port = server.sockets[0].getsockname()[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        status, body = await asyncio.to_thread(
            lambda: (lambda r: (r.status, r.read()))(opener.open(f"http://127.0.0.1:{port}/health", timeout=3)))
        assert (status, body) == (200, b"ok\n")
        async with connect(f"ws://127.0.0.1:{port}/") as ws:
            await ws.send("hi")
            assert await ws.recv() == "hi"
