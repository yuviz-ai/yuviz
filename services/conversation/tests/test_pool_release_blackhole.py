"""A task cancelled inside platform_conn or tenant_conn on a blackholed Postgres must give its
pool slot back.

A tiny in-process server speaks just enough of the protocol to accept connections, answers the
first N simple queries, then goes silent. The control case (acquire_timeout=None) shows the
ROLLBACK after a mid-transaction cancel hangs without terminate-on-cancel.
"""

from __future__ import annotations

import asyncio
import struct

import asyncpg
import pytest

from libs.tenancy.session import platform_conn, tenant_conn

ACQUIRE_TIMEOUT_S = 1.0
MARGIN_S = 1.5


def _msg(kind: bytes, body: bytes = b"") -> bytes:
    return kind + struct.pack("!i", len(body) + 4) + body


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, answer: int) -> None:
    try:
        length, code = struct.unpack("!ii", await reader.readexactly(8))
        if code == 80877103:  # SSLRequest
            writer.write(b"N")
            length, code = struct.unpack("!ii", await reader.readexactly(8))
        await reader.readexactly(length - 8)
        if code == 80877102:  # CancelRequest: never answered
            await asyncio.sleep(3600)
        out = _msg(b"R", struct.pack("!i", 0))
        for key, val in ((b"server_version", b"14.0"), (b"client_encoding", b"UTF8"),
                         (b"integer_datetimes", b"on")):
            out += _msg(b"S", key + b"\0" + val + b"\0")
        writer.write(out + _msg(b"K", struct.pack("!ii", 1, 2)) + _msg(b"Z", b"I"))
        answered = 0
        while True:
            kind = await reader.readexactly(1)
            (length,) = struct.unpack("!i", await reader.readexactly(4))
            await reader.readexactly(length - 4)
            if kind == b"X":
                return
            if kind == b"Q" and answered < answer:
                answered += 1
                writer.write(_msg(b"C", b"OK\0") + _msg(b"Z", b"T"))
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


@pytest.fixture
async def blackhole_server():
    """Factory: start the fake server answering `answer` queries per connection, return its port."""
    servers = []

    async def start(answer: int) -> int:
        try:
            server = await asyncio.start_server(lambda r, w: _serve(r, w, answer), "127.0.0.1", 0)
        except OSError:
            pytest.skip("cannot bind a localhost port")
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield start
    for server in servers:
        server.close()


def _platform(pool, acquire_timeout):
    return platform_conn(pool, reason="test", acquire_timeout=acquire_timeout)


def _tenant(pool, acquire_timeout):
    # The policy resolver's two lookups (fetch and execute_api specialize) both open this.
    return tenant_conn(pool, explicit_tenant="t1", reason="test", acquire_timeout=acquire_timeout)


async def _cancel_mid_flight(port: int, acquire_timeout: float | None, open_conn=_platform):
    """Cancel a connection user while the server is silent; return (task, pool)."""
    pool = await asyncpg.create_pool(
        f"postgresql://u@127.0.0.1:{port}/d", min_size=1, max_size=2, command_timeout=30, ssl=False
    )

    async def work():
        async with open_conn(pool, acquire_timeout) as conn:
            await conn.execute("SELECT hang")

    task = asyncio.ensure_future(work())
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.wait({task}, timeout=ACQUIRE_TIMEOUT_S + MARGIN_S)
    return task, pool


# answer=0: BEGIN itself is never answered; answer=1: BEGIN succeeds, then the first statement
# (SET LOCAL ROLE, or tenant_conn's scope lookup) hangs.
@pytest.mark.parametrize("open_conn", [_platform, _tenant], ids=["platform_conn", "tenant_conn"])
@pytest.mark.parametrize("answer", [0, 1], ids=["blackhole_before_begin", "blackhole_after_begin"])
async def test_cancelled_user_releases_slot_within_bound(blackhole_server, answer, open_conn):
    task, pool = await _cancel_mid_flight(await blackhole_server(answer), ACQUIRE_TIMEOUT_S, open_conn)
    try:
        assert task.done()
        assert task.cancelled()  # the caller sees its own cancellation, not asyncpg's InterfaceError
        assert pool.get_idle_size() == pool.get_size()
        # The slot is usable again: the pool hands out a live connection (closed directly, since
        # a normal release would wait on the silent server).
        conn = await pool.acquire(timeout=1)
        assert not conn.is_closed()
        conn.terminate()
    finally:
        pool.terminate()
async def test_without_acquire_timeout_rollback_hangs_after_begin(blackhole_server):
    task, pool = await _cancel_mid_flight(await blackhole_server(1), None)
    try:
        assert not task.done()
    finally:
        task.cancel()
        pool.terminate()


async def test_exhausted_pool_still_raises_timeout_not_cancellation(blackhole_server):
    """A plain acquire timeout must stay a TimeoutError: asyncpg chains its own internal
    CancelledError onto every timeout, which a caller must never mistake for being cancelled."""
    port = await blackhole_server(0)
    pool = await asyncpg.create_pool(
        f"postgresql://u@127.0.0.1:{port}/d", min_size=1, max_size=1, command_timeout=30, ssl=False
    )
    held = await pool.acquire()
    try:
        async def work():
            async with platform_conn(pool, reason="test", acquire_timeout=0.1):
                pass

        task = asyncio.ensure_future(work())
        await asyncio.wait({task}, timeout=2)
        assert task.done()
        assert not task.cancelled()
        assert isinstance(task.exception(), TimeoutError)
    finally:
        held.terminate()
        pool.terminate()
